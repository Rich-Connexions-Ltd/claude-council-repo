#!/usr/bin/env python3
"""File: scripts/council-review.py
Purpose: Orchestrate the multi-expert Council of Experts plan/code review pipeline (parallel members + consolidator) for the pair-programming workflow.

Role:
  Default reviewer entry point. Reads scripts/council-config.json, fans
  out to configured experts across Codex CLI / Gemini / Claude with
  automatic fallback and retry, enforces a quorum, then consolidates
  findings into REVIEW_Sprint<N>.md and auto-maintains
  FINDINGS_Sprint<N>.md across rounds. Handles sprint-aware diffs,
  convergence guardrails, and secret redaction.

Exports:
  - main, _parse_args -- CLI entrypoint + argparse helper
  - _parse_findings, _read_tracker, _write_tracker -- findings schema
  - _derive_tag, _derive_lens -- deterministic tag/lens derivation
  - LENS_MAP, SOURCE_EXTENSIONS -- shared constants
  - compute_convergence_score, extract_verdict -- review-text helpers
  - find_untracked_source_files, preflight_code_review,
    PreflightResult -- Sprint 2 pre-flight check surface

Last updated: Sprint 6 (2026-04-16) -- metrics v2 schema + helper extraction; selective routing + tracker v3 + security enforcement; output-discipline clause + terse console

Council of Experts review system for pair programming workflow.

Submits plans/code to focused expert reviewers using multiple platforms:
  - Codex CLI (account auth, no API key needed) — primary for most roles
  - Google Gemini (API key) — primary for performance/cost/UX roles
  - Anthropic Claude (API key) — consolidator and fallback

Council composition, models, and phase assignments are configured in
council-config.json.

Usage:
    ./scripts/council-review.py plan <sprint> "<title>"
    ./scripts/council-review.py code <sprint> "<title>"

Requires environment variables (depending on council-config.json):
    GOOGLE_API_KEY      — Gemini models
    ANTHROPIC_API_KEY   — Claude models
    (Codex members authenticate via stored credentials — run 'codex login' once)
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

# Source-file extensions considered for code review materials AND
# pre-flight untracked-file detection. Single source of truth.
SOURCE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".rb", ".swift", ".kt", ".cs", ".cpp", ".c", ".h",
    ".yml", ".yaml", ".toml", ".json", ".sh", ".html", ".css",
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUORUM_THRESHOLD = 3  # Minimum successful council reviews needed

# ---------------------------------------------------------------------------
# Secret Redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}", re.IGNORECASE),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}", re.IGNORECASE),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"xox[bprs]-[A-Za-z0-9\-_]{10,}"),
    # Sprint 6 R1 #21: expand coverage to common credential formats.
    # GitHub classic PATs start with ghp_; fine-grained ones use
    # github_pat_; OAuth app tokens use gho_/ghu_/ghs_/ghr_.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    # AWS access key ID — 20-char alphanumeric prefixed by AKIA/ASIA/AIDA/AGPA/AROA.
    re.compile(r"(?:AKIA|ASIA|AIDA|AGPA|AROA|ANPA|ANVA|AIPA|AIDI)[A-Z0-9]{16}"),
    # AWS secret access key — 40-char base64ish that appears after
    # aws_secret_access_key= or in JSON "SecretAccessKey":"...".
    re.compile(
        r"(?:aws_secret_access_key|secretaccesskey)\s*[=:]\s*['\"]?[A-Za-z0-9+/=]{40}['\"]?",
        re.IGNORECASE,
    ),
    # Compound assignment names: catches FOO_API_KEY=..., SERVICE_SECRET_KEY=...,
    # PROJECT_TOKEN=..., AUTH_TOKEN=..., *_PRIVATE_KEY=..., *_CREDENTIALS=...
    re.compile(
        r"(?:[A-Z0-9_]+_)?"
        r"(?:API[_-]?KEY|API[_-]?TOKEN|AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|"
        r"SECRET[_-]?KEY|SECRET|PASSWORD|PASSPHRASE|PRIVATE[_-]?KEY|"
        r"CREDENTIALS|CREDENTIAL|TOKEN)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9\-_\.+/=]{8,}['\"]?",
        re.IGNORECASE,
    ),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.+/=]{8,}", re.IGNORECASE),
]


METRICS_SCHEMA_VERSION = 2
METRICS_SCHEMA_KEY = "_schema"
METRICS_SCHEMA_VALUE = "council_metrics"


def _compute_findings_by_lens(findings: list[dict]) -> dict[str, int]:
    """Count findings grouped by lens. Returns a plain dict so it
    serialises cleanly as JSON. Findings with missing/empty lens are
    bucketed under ``unknown`` to match tracker load-time defaults."""
    counts: dict[str, int] = {}
    for f in findings:
        lens = (f.get("lens") or "unknown") or "unknown"
        counts[lens] = counts.get(lens, 0) + 1
    return counts


def _compute_findings_counts(findings: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    by_sev = {"high": 0, "medium": 0, "low": 0}
    by_status = {
        "ADDRESSED": 0, "WONTFIX": 0, "OPEN": 0,
        "RECURRING": 0, "VERIFIED": 0, "REOPENED": 0,
    }
    for f in findings:
        sev = (f.get("severity") or "").lower()
        if sev in by_sev:
            by_sev[sev] += 1
        st = (f.get("status") or "").upper()
        if st in by_status:
            by_status[st] += 1
    return by_sev, by_status


def _write_metrics_row(out_file: Path, row: dict) -> None:
    """Append a row to the metrics JSONL. Writes the schema sentinel
    as the first line of a new file; appends only data rows thereafter.
    The sentinel row has the shape
    ``{"_schema": "council_metrics", "version": N}`` and is never
    interleaved with data rows."""
    out_file.parent.mkdir(exist_ok=True)
    write_header = not out_file.exists() or out_file.stat().st_size == 0
    with out_file.open("a", encoding="utf-8") as fh:
        if write_header:
            fh.write(json.dumps({
                METRICS_SCHEMA_KEY: METRICS_SCHEMA_VALUE,
                "version": METRICS_SCHEMA_VERSION,
            }) + "\n")
        fh.write(json.dumps(row) + "\n")


def _emit_metrics(
    repo_root: Path,
    sprint: str,
    review_type: str,
    round_num: int,
    *,
    members_active: int,
    members_succeeded: int,
    elapsed_s: float,
    verdict: str | None,
    tracker_file: Path,
    security_bypassed: bool = False,
) -> None:
    """Append one per-round metrics row. Helpers own the schema math;
    this function is the wiring layer."""
    findings: list[dict] = []
    if tracker_file.exists():
        try:
            findings = _read_tracker(tracker_file)
        except Exception:  # noqa: BLE001
            findings = []
    by_sev, by_status = _compute_findings_counts(findings)
    record = {
        "sprint": sprint,
        "review_type": review_type,
        "round": round_num,
        "members_active": members_active,
        "members_succeeded": members_succeeded,
        "elapsed_seconds": round(elapsed_s, 2),
        "findings_total": len(findings),
        "findings_high": by_sev["high"],
        "findings_medium": by_sev["medium"],
        "findings_low": by_sev["low"],
        "findings_addressed": by_status["ADDRESSED"],
        "findings_wontfix": by_status["WONTFIX"],
        "findings_open": by_status["OPEN"],
        "findings_verified": by_status["VERIFIED"],
        "findings_reopened": by_status["REOPENED"],
        "findings_recurring": by_status["RECURRING"],
        "findings_by_lens": _compute_findings_by_lens(findings),
        "security_bypassed": bool(security_bypassed),
        "verdict": verdict or "UNKNOWN",
    }
    _write_metrics_row(
        repo_root / "council" / f"metrics_Sprint{sprint}.jsonl", record
    )


def redact_secrets(text: str) -> str:
    """Redact common secret patterns from text before sending to external APIs."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Environment — source API keys from ~/.zprofile if not already in env
# ---------------------------------------------------------------------------


def ensure_api_keys_from_profile():
    """Source API keys from ~/.zprofile if they're missing from the environment."""
    zprofile = Path.home() / ".zprofile"
    if not zprofile.exists():
        return

    needed = {"GOOGLE_API_KEY", "ANTHROPIC_API_KEY"}
    missing = {k for k in needed if not os.environ.get(k)}
    if not missing:
        return

    try:
        for line in zprofile.read_text().splitlines():
            line = line.strip()
            if not line.startswith("export "):
                continue
            rest = line[len("export "):]
            if "=" not in rest:
                continue
            key, _, value = rest.partition("=")
            key = key.strip()
            value = value.strip()
            # Sprint 6 R1 #22: strip inline comments before stripping
            # quotes, so `FOO=bar  # my api key` doesn't end up as
            # `bar  # my api key`. The simple heuristic is "# preceded
            # by whitespace" — it avoids mangling values that legitimately
            # contain #, like base64 or URL anchors, because those
            # aren't preceded by whitespace.
            if " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            elif "\t#" in value:
                value = value.split("\t#", 1)[0].rstrip()
            value = value.strip('"').strip("'")
            if key in missing and value:
                os.environ[key] = value
                missing.discard(key)
                print(f"  [env] Sourced {key} from ~/.zprofile", file=sys.stderr)
    except Exception as e:
        print(f"  [env] Warning: could not parse ~/.zprofile: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(config_path: Path) -> dict:
    """Load council configuration from JSON file."""
    if not config_path.exists():
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def get_active_members(
    config: dict,
    review_type: str,
    *,
    lenses: set[str] | None = None,
) -> list[dict]:
    """Return only council members whose phases include the review type.

    When ``lenses`` is non-None (only valid for code reviews), the
    filter is narrowed to members whose ``role`` is in the set. The
    caller is responsible for validating lens names and for enforcing
    the security-non-removable rule before passing ``lenses`` here.
    """
    members = [
        m for m in config["council"]["members"]
        if review_type in m.get("phases", ["plan", "code"])
    ]
    if lenses is not None:
        members = [m for m in members if m.get("role") in lenses]
    return members


# ---------------------------------------------------------------------------
# Selective routing — Sprint 6
# ---------------------------------------------------------------------------


class LensArgError(Exception):
    """Raised when a --lenses or --auto-lenses invocation is malformed
    or violates the security-non-removable rule."""


def _known_lens_roles(config: dict) -> list[str]:
    return sorted({m["role"] for m in config["council"]["members"]})


def parse_lenses_arg(raw: str, valid_roles: list[str]) -> set[str]:
    """Parse the comma-separated ``--lenses`` value. Rejects degenerate
    inputs (empty string, lone commas, whitespace-only, blank entries,
    duplicates, unknown names) with a specific message per R1 #9.

    Returns the parsed set on success; raises ``LensArgError`` on any
    failure. Callers surface the message to stderr and exit 2.
    """
    stripped = raw.strip()
    if not stripped:
        raise LensArgError("--lenses value is empty")
    if stripped in (",", ",,"):
        raise LensArgError("--lenses value is a lone comma")
    parts = raw.split(",")
    seen: list[str] = []
    for part in parts:
        token = part.strip()
        if not token:
            raise LensArgError(
                f"--lenses contains an empty entry (near {part!r})"
            )
        if token in seen:
            raise LensArgError(
                f"--lenses contains a duplicate entry: {token!r}"
            )
        seen.append(token)
    unknown = [t for t in seen if t not in valid_roles]
    if unknown:
        raise LensArgError(
            f"--lenses contains unknown lens(es) {unknown}. "
            f"Valid lenses: {valid_roles}"
        )
    return set(seen)


def auto_lens_set(changed_paths: list[str], valid_roles: list[str]) -> set[str]:
    """Compute the auto-routed lens set from a list of changed file
    paths. Rules:

    - ``security`` is always included.
    - ``code_quality`` is always included (always-on keeps the lens
      honest against duplication / complexity regressions).
    - ``test_quality`` is included when any path is under ``tests/``.
    - ``domain`` is included when any path is under ``knowledge/``.

    Lenses not present in ``valid_roles`` are dropped silently — the
    council may not have every seat configured.
    """
    lenses = {"security", "code_quality"}
    for path in changed_paths:
        if path.startswith("tests/"):
            lenses.add("test_quality")
        if path.startswith("knowledge/"):
            lenses.add("domain")
    return lenses & set(valid_roles)


def enforce_security_lens(
    lenses: set[str],
    *,
    allow_no_security: bool,
    review_type: str,
) -> tuple[set[str], bool]:
    """Enforce the security-non-removable rule for code reviews.

    Returns ``(lenses, security_bypassed)``. Raises ``LensArgError``
    when ``security`` is absent and ``allow_no_security`` was not
    passed — the fail-closed default prevents a code review from
    reaching APPROVED with no security reviewer.

    Sprint 6 R1 #17: also rejects any non-empty ``lenses`` for plan
    reviews at the shared-logic layer, not just at the argparse
    layer. Internal callers can no longer silently get partial lens
    selection for plan reviews.
    """
    if review_type not in {"plan", "code"}:
        raise LensArgError(
            f"unknown review_type {review_type!r}; expected "
            "'plan' or 'code'"
        )
    if review_type == "plan":
        if lenses:
            raise LensArgError(
                "plan reviews run every active member; "
                "--lenses is not accepted for plan phase."
            )
        return lenses, False
    if "security" in lenses:
        return lenses, False
    if not allow_no_security:
        raise LensArgError(
            "code reviews require the security lens. Either add "
            "'security' to --lenses or pass --allow-no-security to "
            "explicitly acknowledge the bypass."
        )
    return lenses, True


def validate_api_keys(config: dict, active_members: list[dict]) -> dict[str, str]:
    """Validate API keys required by active members + consolidator."""
    required = set()
    optional = set()
    for member in active_members:
        env = member.get("api_key_env")
        if env:
            required.add(env)
        fallback = member.get("fallback")
        if fallback and fallback.get("api_key_env"):
            optional.add(fallback["api_key_env"])

    consolidator = config["council"]["consolidator"]
    cons_env = consolidator.get("api_key_env")
    if cons_env:
        required.add(cons_env)
    consolidator_fb = consolidator.get("fallback")
    if consolidator_fb and consolidator_fb.get("api_key_env"):
        optional.add(consolidator_fb["api_key_env"])

    keys = {}
    missing = []
    for env_var in sorted(required):
        val = os.environ.get(env_var)
        if val:
            keys[env_var] = val
        else:
            missing.append(env_var)

    if missing:
        print(f"ERROR: Missing required API key(s): {', '.join(missing)}", file=sys.stderr)
        print("Set them in your environment before running council review.", file=sys.stderr)
        sys.exit(1)

    for env_var in sorted(optional - required):
        val = os.environ.get(env_var)
        if val:
            keys[env_var] = val

    return keys


# ---------------------------------------------------------------------------
# API Clients
# ---------------------------------------------------------------------------


def call_google(
    model: str, contents: str,
    max_tokens: int, temperature: float, api_key: str, timeout: float,
) -> str:
    """Call Google GenAI API."""
    from google import genai
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config={"max_output_tokens": max_tokens, "temperature": temperature},
    )
    text = response.text
    if text is None:
        raise RuntimeError("Google API returned empty/blocked response (safety filter or quota exceeded)")
    return text


def call_anthropic(
    model: str, system: str, user_content: str,
    max_tokens: int, temperature: float, api_key: str, timeout: float,
) -> str:
    """Call Anthropic API."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text


def call_codex(
    system: str, user_content: str, timeout: float,
    review_mode: bool = False,
) -> str:
    """Call Codex CLI using account auth.

    Sprint 5: previously wrote the combined prompt to a NamedTemporaryFile
    that the subprocess never read (the prompt is piped via stdin via
    ``input=``). Removing the tempfile eliminates a dead-code leak path
    — if the write had raised, ``prompt_file`` would have been unbound
    and the ``finally`` unlink would have masked the original error with
    a NameError.
    """
    combined_prompt = f"{system}\n\n---\n\n{user_content}"

    try:
        # Always use 'codex exec --full-auto' — the 'codex review' subcommand
        # in v0.114.0+ no longer accepts positional prompt arguments or stdin.
        cmd = ["codex", "exec", "--full-auto"]
        result = subprocess.run(
            cmd,
            input=combined_prompt,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError("Codex CLI not found. Install: npm install -g @openai/codex")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Codex timed out after {timeout:.0f}s")

    if result.returncode != 0:
        stderr_first_line = (result.stderr or "").split("\n")[0][:120]
        print(f"  [debug] Codex stderr: {stderr_first_line}", file=sys.stderr)
        raise RuntimeError(f"Codex exited {result.returncode}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Codex produced no output")
    return output


def call_claude_cli(
    system: str, user_content: str, timeout: float, model: str = "sonnet",
) -> str:
    """Call the local `claude` CLI in non-interactive mode (Sprint 127 v6).

    `claude -p "<prompt>"` runs a one-shot Claude Code invocation from the
    current working directory. When cwd is the repo root, the CLI
    auto-loads `.mcp.json` so the reviewer inherits the project's MCP tool
    suite (including `codegraph_*`). Output is captured from
    stdout. The CLI handles auth from the user's existing Claude Code
    session; no API key required.

    `model` selects the Claude model alias (e.g. "sonnet", "opus", "haiku").
    """
    combined_prompt = f"{system}\n\n---\n\n{user_content}"

    # Write prompt to a temp file to avoid shell-quoting issues with long
    # markdown content containing backticks, quotes, dollar signs, etc.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(combined_prompt)
        prompt_file = f.name

    try:
        # Sprint 6 R1 #14: use `plan` mode instead of
        # `bypassPermissions`. Reviewers use read-only MCP tools
        # (codegraph_*) but must NOT edit the repo. `plan` permits
        # tool use for queries while blocking writes, matching the
        # reviewer's read-only role. Repository content enters the
        # prompt via the stdin-piped `combined_prompt`, which is
        # treated as reference material, not instructions.
        cmd = ["claude", "-p", "--model", model, "--permission-mode", "plan"]
        with open(prompt_file, "r") as inp:
            result = subprocess.run(
                cmd,
                stdin=inp,
                capture_output=True, text=True, timeout=timeout,
            )
    except FileNotFoundError:
        raise RuntimeError(
            "claude CLI not found. Install from https://claude.ai/download"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {timeout:.0f}s")
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass

    if result.returncode != 0:
        stderr_first_line = (result.stderr or "").split("\n")[0][:200]
        print(f"  [debug] claude stderr: {stderr_first_line}", file=sys.stderr)
        raise RuntimeError(f"claude CLI exited {result.returncode}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude CLI produced no output")
    return output


def call_model(
    platform: str, model: str, system: str, user_content: str,
    max_tokens: int, temperature: float, api_key: str, timeout: float,
    review_mode: bool = False,
) -> str:
    """Dispatch to the appropriate platform API."""
    if platform == "google":
        combined = f"{system}\n\n---\n\n{user_content}"
        return call_google(model, combined, max_tokens, temperature, api_key, timeout)
    elif platform == "anthropic":
        return call_anthropic(model, system, user_content, max_tokens, temperature, api_key, timeout)
    elif platform == "codex":
        return call_codex(system, user_content, timeout, review_mode=review_mode)
    elif platform == "claude_cli":
        return call_claude_cli(system, user_content, timeout, model=model)
    else:
        raise ValueError(f"Unknown platform: {platform}")


# ---------------------------------------------------------------------------
# Material Gathering
# ---------------------------------------------------------------------------


def read_file_safe(path: Path, max_lines: int = 500) -> str:
    """Read a file, truncating if too long."""
    if not path.exists():
        return f"[File not found: {path}]"
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n\n[... truncated, {len(lines)} total lines]"
        return "\n".join(lines)
    except Exception as e:
        return f"[Error reading {path}: {e}]"


def get_changed_files(sprint: str | None = None, repo_root: Path | None = None) -> list[str]:
    """Get list of changed files for code review."""
    # Strategy 1: Sprint-aware diff from recorded base commit
    if sprint and repo_root:
        base_file = repo_root / f".sprint-base-commit-{sprint}"
        if base_file.exists():
            base_sha = base_file.read_text().strip()
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_sha}..HEAD"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                files = [f for f in result.stdout.strip().split("\n") if f]
                if files:
                    return files

    # Strategy 2: Uncommitted changes
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        files = [f for f in result.stdout.strip().split("\n") if f]
        if files:
            return files

    # Strategy 3: Recent commits
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~10..HEAD"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        files = [f for f in result.stdout.strip().split("\n") if f]
        if files:
            return files

    # Strategy 4: Parse plan file for expected files
    if sprint and repo_root:
        plan_file = repo_root / f"PLAN_Sprint{sprint}.md"
        if plan_file.exists():
            files = _parse_plan_file_list(plan_file)
            if files:
                return files

    return []


def _parse_plan_file_list(plan_file: Path) -> list[str]:
    """Extract file paths from the 'Files to Create/Modify' table in a PLAN file."""
    in_table = False
    files = []
    for line in plan_file.read_text().splitlines():
        if "Files to Create/Modify" in line or "Files Changed" in line:
            in_table = True
            continue
        if in_table:
            if line.startswith("|") and "`" in line:
                parts = line.split("`")
                if len(parts) >= 2:
                    path = parts[1].strip()
                    if path and not path.startswith("--"):
                        files.append(path)
            elif line.strip() == "" or line.startswith("#"):
                in_table = False
    return files


def gather_plan_materials(sprint: str, repo_root: Path) -> str:
    """Gather materials for a plan review."""
    sections = []

    plan_file = repo_root / f"PLAN_Sprint{sprint}.md"
    if plan_file.exists():
        content = read_file_safe(plan_file, max_lines=1000)
        sections.append(f"### {plan_file.name} (PRIMARY — this is what you are reviewing)\n```\n{content}\n```")
    else:
        print(f"ERROR: Plan file not found: {plan_file}", file=sys.stderr)
        sys.exit(1)

    changes_file = repo_root / "CHANGES.md"
    if changes_file.exists():
        content = read_file_safe(changes_file, max_lines=200)
        sections.append(f"### CHANGES.md (project history)\n```\n{content}\n```")

    history_file = repo_root / "Documentation" / "PLAN_history.md"
    if history_file.exists():
        content = read_file_safe(history_file, max_lines=300)
        sections.append(f"### Documentation/PLAN_history.md (prior decisions, truncated)\n```\n{content}\n```")

    # --- Codebase structure context for files mentioned in the plan ---
    plan_files = _parse_plan_file_list(plan_file)
    if plan_files:
        # Only include files that already exist (Modify, not Create)
        existing_files = [f for f in plan_files if (repo_root / f).exists()]
        if existing_files:
            codegraph_context = _generate_codegraph_context(existing_files, repo_root)
            if codegraph_context:
                sections.append(codegraph_context)

    return "\n\n".join(sections)


def _generate_codegraph_context(source_files: list[str], repo_root: Path) -> str | None:
    """Generate codebase structure context from the semantic index DB.

    Calls scripts/index-codebase.py --context-for with the changed file list.
    Returns a markdown section, or None if the DB doesn't exist or the call fails.
    """
    db_path = repo_root / ".claude" / "codebase.db"
    if not db_path.exists() or not source_files:
        return None

    try:
        import subprocess
        result = subprocess.run(
            ["python3", str(repo_root / "scripts" / "index-codebase.py"),
             "--context-for"] + source_files,
            capture_output=True, text=True, timeout=15, cwd=str(repo_root)
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return None


def _filename_is_safe(rel: str) -> bool:
    """Reject filenames with control characters that would enable
    prompt-injection or newline-tokenisation bugs in review materials."""
    if "\x00" in rel or "\n" in rel or "\r" in rel:
        return False
    return all(ord(c) >= 0x20 and ord(c) != 0x7f for c in rel)


def find_untracked_source_files(repo_root: Path) -> list[str]:
    """Return untracked source files (suffix in SOURCE_EXTENSIONS).

    Filenames containing control characters are dropped with a stderr
    warning. Returns repo-relative paths sorted deterministically.
    Non-git directories return an empty list.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=str(repo_root),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    out: list[str] = []
    for f in result.stdout.split("\n"):
        f = f.strip()
        if not f:
            continue
        if not _filename_is_safe(f):
            print(f"preflight: refusing unsafe filename {f!r}", file=sys.stderr)
            continue
        if Path(f).suffix in SOURCE_EXTENSIONS:
            out.append(f)
    return sorted(out)


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of the code-review pre-flight check.

    Field invariants:
      - When `ok` is False, `banner` is empty and `reject_message` is
        non-empty (printed to stderr by main(), exit 4).
      - When `ok` is True and untracked files exist (allow_untracked),
        `banner` is non-empty (injected as the first section of
        review materials) and `reject_message` is empty.
      - On a clean tree, `ok=True` with both `banner` and
        `reject_message` empty.
    """
    ok: bool
    banner: str
    reject_message: str


def preflight_code_review(
    repo_root: Path, allow_untracked: bool
) -> PreflightResult:
    """Detect untracked source files and decide whether to proceed.

    Side-effect-free (except for the sanitiser warning inside
    find_untracked_source_files). main() is responsible for printing
    reject_message and setting exit code 4 on ok=False.
    """
    untracked = find_untracked_source_files(repo_root)
    if not untracked:
        return PreflightResult(ok=True, banner="", reject_message="")
    if not allow_untracked:
        lines = [
            f"Error: {len(untracked)} untracked source file(s) will not appear in review materials:",
            *(f"  {i + 1}. {shlex.quote(f)}" for i, f in enumerate(untracked)),
            "",
            "Commit them before running code review (they'll diff against",
            ".sprint-base-commit-<N>):",
            "  git add <files> && git commit -m 'Sprint N: <summary>'",
            "",
            "Override with --allow-untracked (not recommended for final review).",
        ]
        return PreflightResult(
            ok=False, banner="", reject_message="\n".join(lines),
        )
    banner_lines = [
        "=== PRE-FLIGHT BANNER ===",
        f"⚠ Review includes {len(untracked)} uncommitted source file(s):",
        *(f"  - {shlex.quote(f)}" for f in untracked),
        "=== END BANNER ===",
    ]
    return PreflightResult(
        ok=True, banner="\n".join(banner_lines), reject_message="",
    )


def _render_source_file(path: str, repo_root: Path) -> str:
    """Render one source file as a code-fenced section. Shared between
    tracked and untracked rendering so they can't diverge."""
    full_path = repo_root / path
    content = read_file_safe(full_path, max_lines=300)
    ext = Path(path).suffix.lstrip(".")
    return f"### {path}\n```{ext}\n{content}\n```"


def gather_code_materials(
    sprint: str, repo_root: Path,
    banner: str = "",
    include_untracked: bool = False,
) -> str:
    """Gather materials for a code review.

    When ``banner`` is non-empty it is prepended as the first section
    (clearly delimited). When ``include_untracked`` is True, untracked
    source files are appended to the tracked file list and rendered
    the same way.
    """
    sections = []
    if banner:
        sections.append(banner)

    plan_file = repo_root / f"PLAN_Sprint{sprint}.md"
    if plan_file.exists():
        content = read_file_safe(plan_file, max_lines=700)
        sections.append(f"### {plan_file.name} (approved plan)\n```\n{content}\n```")

    changes_file = repo_root / "CHANGES.md"
    if changes_file.exists():
        content = read_file_safe(changes_file, max_lines=200)
        sections.append(f"### CHANGES.md\n```\n{content}\n```")

    changed_files = get_changed_files(sprint=sprint, repo_root=repo_root)
    source_files = [
        f for f in changed_files
        if Path(f).suffix in SOURCE_EXTENSIONS
        and not f.startswith("Documentation/")
        and "PLAN_" not in f
        and "REVIEW_" not in f
    ]

    if include_untracked:
        # Recompute here is intentional: gather_code_materials is a
        # leaf-callable (tested independently of preflight_code_review).
        # The cost is a single `git ls-files` call; negligible.
        untracked = find_untracked_source_files(repo_root)
        for f in untracked:
            if f not in source_files:
                source_files.append(f)

    for f in source_files[:25]:
        sections.append(_render_source_file(f, repo_root))

    # Compute the insertion anchor so the banner (if any) stays first.
    anchor = 1 if banner else 0

    if changed_files:
        file_list = "\n".join(f"- {f}" for f in changed_files)
        sections.insert(anchor, f"### Changed Files\n{file_list}")
        anchor += 1

    # --- Codebase structure context from the semantic index ---
    codegraph_context = _generate_codegraph_context(source_files, repo_root)
    if codegraph_context:
        sections.insert(anchor, codegraph_context)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Prompt Construction
# ---------------------------------------------------------------------------


def _round_context(round_num: int) -> str:
    if round_num == 1:
        return "This is the first review of this plan."
    return (
        f"This is round {round_num}. The artifact has been revised to address "
        f"findings from previous rounds.\n\n"
        f"FOCUS on:\n"
        f"1. Whether previous findings have been adequately addressed\n"
        f"2. Any genuinely NEW issues introduced by the revisions\n\n"
        f"Do NOT re-raise findings that have been marked ADDRESSED in the tracker "
        f"unless the fix is demonstrably incomplete. Do NOT introduce novel concerns "
        f"about previously-reviewed sections that haven't changed."
    )


def _tracker_section(tracker_content: str | None, round_num: int) -> str:
    if not tracker_content or round_num <= 1:
        return ""
    return f"""

## Prior Findings Tracker (MUST READ before writing findings)

Findings below have already been resolved in this sprint. You MUST NOT re-flag
a finding that is ADDRESSED, WONTFIX, or RECURRING unless you have SPECIFIC
NEW EVIDENCE the prior resolution is wrong.

- ADDRESSED: the editor fixed the finding. Do not re-flag unless you can
  demonstrate the fix is incomplete or regressed.
- WONTFIX: the finding was rejected with a justification in the Resolution
  column. Do not re-flag unless you can refute that justification with
  concrete evidence (e.g. an MCP query result contradicting it). Cite the
  prior round number and quote the specific sentence you're refuting.
- RECURRING: the item has been flagged 3+ times; it is accepted as Known
  Debt. Do not re-flag.

Before writing each new finding, check the tracker. Re-flags that ignore the
prior Resolution column without new evidence will be excluded by the
consolidator.

{tracker_content}
"""


def _output_discipline_clause(role: str) -> str:
    """Sprint 6 R2 #24: extracted so build_council_prompt stays under
    the 60-line guideline. Security reviews get a higher advisory
    budget to accommodate MCP-query evidence text."""
    if role == "security":
        advisory = 2500
        note = (
            " Security reviews commonly cite MCP query results; the "
            "higher budget accommodates evidence text."
        )
    else:
        advisory = 1500
        note = ""
    return f"""## Output discipline

- Short declarative sentences. No hedging ("perhaps", "might
  consider", "it would be advisable").
- Do not restate findings in any assessment or summary section —
  the Findings block is authoritative.
- No motivational framing ("great work!", "overall, this is
  solid"). Findings-only.
- Advisory budget: aim for ≤ {advisory} tokens.{note}
  If you exceed the budget, emit a final line
  `[TRUNCATED: N findings omitted]` so the consolidator can flag
  the gap to the human. **Never silently drop findings.**"""


def _council_output_format(role: str, label: str, sprint: str, round_num: int, review_type_label: str) -> str:
    return f"""## Output Format

Write your review in EXACTLY this structure:

### {role} Review: Sprint {sprint} (R{round_num})

**Scope:** {label}

#### Findings
List findings ONLY within your area of focus. For each finding, include the file path and location:
- **[High]** description (File: `path/to/file`, Location: function_name or line range)
  - Current: what exists now
  - Fix: specific action to take
- **[Medium]** description (File: `path/to/file`, Location: function_name or line range)
  - Current: what exists now
  - Fix: specific action to take
- **[Low]** description (File: `path/to/file` if applicable)

If you find NO issues in your area, write: "No findings in this area."

#### Assessment
A 2-3 sentence overall assessment of the {review_type_label} from your expert perspective.

IMPORTANT:
- Stay strictly within your area of expertise
- Do NOT comment on areas outside your lens
- Be specific: cite file paths, line numbers (for code), or section names (for plans)
- For each finding, explain WHAT is wrong AND HOW to fix it
- Be EXHAUSTIVE in Round 1: list ALL concerns you can identify in a single pass. The goal is zero new findings from your area in R2+.
- In Round 2+: do NOT re-flag ADDRESSED or RECURRING items from the tracker"""


def build_council_prompt(
    member: dict, materials: str,
    sprint: str, title: str, round_num: int, review_type: str,
    tracker_content: str | None = None,
) -> tuple[str, str]:
    """Build system + user prompts for a council member. Returns (system, user).

    Sprint 6 R2 #24: split into helpers (_round_context, _tracker_section,
    _output_discipline_clause, _council_output_format) so this function
    stays under the 60-line complexity guideline.
    """
    role = member["role"]
    label = member["label"]
    lens = member["lens"]
    review_type_label = "plan" if review_type == "plan" else "code implementation"

    system_prompt = f"""You are {label} on a review council for a pair programming workflow.

## Your Review Lens
{lens}

{_output_discipline_clause(role)}"""

    user_prompt = f"""## Review Type
This is a {review_type_label} review for Sprint {sprint}: {title} (Round {round_num}).
{_round_context(round_num)}
{_tracker_section(tracker_content, round_num)}
## Materials Under Review
{materials}

{_council_output_format(role, label, sprint, round_num, review_type_label)}"""

    return system_prompt, user_prompt


def build_consolidator_prompt(
    council_reviews: dict[str, str],
    sprint: str, title: str, round_num: int, review_type: str,
    member_labels: dict[str, str],
    tracker_content: str | None = None,
    escalation_note: str | None = None,
) -> tuple[str, str]:
    """Build system + user prompts for the consolidator."""
    review_type_cap = "Plan" if review_type == "plan" else "Code"

    review_sections = []
    for role, review_text in council_reviews.items():
        label = member_labels.get(role, role.title())
        review_sections.append(f"### {label}\n{review_text}")
    all_reviews = "\n\n---\n\n".join(review_sections)

    successful_count = sum(1 for r in council_reviews.values() if "UNAVAILABLE" not in r)
    assessment_sections = _consolidator_assessment_sections(review_type)
    tracker_section = _consolidator_tracker_section(tracker_content, round_num)
    escalation_section = f"\n{escalation_note}\n" if escalation_note else ""

    system_prompt = _CONSOLIDATOR_SYSTEM_PROMPT
    user_prompt = _consolidator_user_prompt(
        all_reviews=all_reviews,
        tracker_section=tracker_section,
        escalation_section=escalation_section,
        review_type=review_type,
        review_type_cap=review_type_cap,
        sprint=sprint,
        title=title,
        round_num=round_num,
        successful_count=successful_count,
        assessment_sections=assessment_sections,
    )
    return system_prompt, user_prompt


_CONSOLIDATOR_SYSTEM_PROMPT = """You are the Consolidation Lead for a review council. Multiple domain experts have independently reviewed a plan or implementation. Your job is to synthesise their findings into a single, coherent review with one verdict.

## Output discipline

- Short declarative sentences. No hedging ("perhaps", "might
  consider", "it would be advisable").
- Do not restate findings in any assessment or summary section —
  the Findings block is authoritative.
- No motivational framing ("great work!", "overall, this is
  solid"). Findings-only.
- Advisory budget: aim for ≤ 3000 tokens for the consolidated review.
  If you exceed the budget, emit a final line
  `[TRUNCATED: N findings omitted]` so the human editor sees the gap.
  **Never silently drop findings.**"""


def _consolidator_assessment_sections(review_type: str) -> str:
    if review_type == "plan":
        return """### Design Assessment
[Synthesised evaluation of the proposed approach]

### Completeness
[Does the plan cover all deliverables and edge cases?]"""
    return """### Implementation Assessment
[Does the code correctly implement the approved plan?]

### Code Quality
[Synthesised assessment of clarity, documentation, error handling]

### Test Coverage
[Synthesised assessment of test adequacy]"""


def _consolidator_tracker_section(tracker_content: str | None, round_num: int) -> str:
    if not tracker_content or round_num <= 1:
        return ""
    return f"""

## Prior Findings Tracker
Items marked ADDRESSED have been fixed. Do NOT re-flag ADDRESSED items unless the fix is demonstrably incomplete.

{tracker_content}
"""


def _consolidator_user_prompt(
    *, all_reviews: str, tracker_section: str, escalation_section: str,
    review_type: str, review_type_cap: str, sprint: str, title: str,
    round_num: int, successful_count: int, assessment_sections: str,
) -> str:
    """Sprint 6 R2 #26: extracted from build_consolidator_prompt so
    the public function stays under the 60-line complexity guideline."""
    verdict_options = (
        "APPROVED | CHANGES_REQUESTED | PLAN_REVISION_REQUIRED"
        if review_type == "code"
        else "APPROVED | CHANGES_REQUESTED"
    )
    plan_revision_section = (
        "### Plan Revisions (if PLAN_REVISION_REQUIRED)\n"
        "[What needs to change in the plan]\n"
        if review_type == "code"
        else ""
    )
    return f"""## Council Reviews

{all_reviews}
{tracker_section}{escalation_section}
## Consolidation Instructions

1. **Identify overlapping concerns**: Merge findings on the same underlying issue across experts.
2. **Resolve conflicts**: If experts disagree, use judgement to determine which concern dominates.
3. **Filter false positives**: Exclude speculative or out-of-lens findings.
4. **Assign final severity**:
   - [High]: Would cause a bug, security vulnerability, data loss, or spec violation. Blocks approval.
   - [Medium]: Would cause maintainability, performance, or usability problems.
   - [Low]: Improvement suggestion. Optional.
5. **Determine verdict**:
   - APPROVED: Zero [High] findings AND overall design/implementation is sound
   - CHANGES_REQUESTED: One or more [High] findings, OR three or more [Medium] in same area
   - PLAN_REVISION_REQUIRED (code reviews only): Fundamental design flaw discovered during implementation

## Output Format

## {review_type_cap} Review: Sprint {sprint} - {title} (R{round_num})

**Round:** {round_num}
**Verdict:** {verdict_options}
**Review Method:** Council of Experts ({successful_count} reviewers + consolidator)

{assessment_sections}

### Findings
- **[High]** description (File: `path/to/file`, Location: function_name) (Source: expert_name)
- **[Medium]** description (Source: expert_name)
- **[Low]** description (Source: expert_name)

### Excluded Findings
- description — Reason: why excluded (Source: expert_name)
[If none, write "No findings excluded."]

### Required Changes (if CHANGES_REQUESTED)
For each required change:
1. **File**: exact file path
   **Location**: function/class name or line range
   **Current behavior**: what exists now
   **Required change**: exactly what must change
   **Acceptance criteria**: how to verify the fix

{plan_revision_section}### Recommendations
[Consolidated optional improvements]

### Expert Concordance
| Area | Experts Agreeing | Key Theme |
|------|-----------------|-----------|
| ... | ... | ... |"""


# ---------------------------------------------------------------------------
# Council Execution
# ---------------------------------------------------------------------------

_codex_call_index = 0
_codex_call_lock = None


def _call_member(
    platform: str, model: str, api_key_env: str,
    system_prompt: str, user_prompt: str,
    max_tokens: int, temperature: float,
    api_keys: dict, timeout: float,
    review_mode: bool = False,
) -> str:
    """Make a single API call for a council member."""
    api_key = None if platform == "codex" else api_keys.get(api_key_env, "")
    return call_model(
        platform=platform,
        model=model,
        system=system_prompt,
        user_content=user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        api_key=api_key,
        timeout=timeout,
        review_mode=review_mode,
    )


def run_council_member(
    member: dict, materials: str, api_keys: dict,
    sprint: str, title: str, round_num: int, review_type: str,
    timeout: float,
    codex_stagger: float = 0,
    retry_delay: float = 5,
    tracker_content: str | None = None,
) -> tuple[str, str, float]:
    """Run a single council member with retry + fallback. Returns (role, review_text, elapsed_seconds)."""
    global _codex_call_index, _codex_call_lock

    role = member["role"]
    start = time.monotonic()

    system_prompt, user_prompt = build_council_prompt(
        member, materials, sprint, title, round_num, review_type,
        tracker_content=tracker_content,
    )

    review_mode = (review_type == "code")

    if member["platform"] == "codex" and codex_stagger > 0 and _codex_call_lock:
        import threading
        with _codex_call_lock:
            idx = _codex_call_index
            _codex_call_index += 1
        delay = idx * codex_stagger
        if delay > 0:
            print(f"    {member['label']:25s} stagger {delay:.0f}s...", file=sys.stderr)
            time.sleep(delay)

    primary_err = None
    for attempt in range(2):
        try:
            review = _call_member(
                member["platform"], member["model"], member["api_key_env"],
                system_prompt, user_prompt,
                member["max_tokens"], member["temperature"],
                api_keys, timeout,
                review_mode=review_mode,
            )
            elapsed = time.monotonic() - start
            return role, review, elapsed
        except Exception as err:
            primary_err = err
            primary_type = type(err).__name__
            if attempt == 0:
                print(f"  [debug] {member['label']} attempt 1 failed ({primary_type}), retrying in {retry_delay}s...", file=sys.stderr)
                time.sleep(retry_delay)
            else:
                print(f"  [debug] {member['label']} attempt 2 failed ({primary_type}): {err}", file=sys.stderr)

    fallback = member.get("fallback")
    fb_key_env = fallback.get("api_key_env") if fallback else None
    fb_available = fallback and (fallback.get("platform") == "codex" or fb_key_env in api_keys)
    primary_type = type(primary_err).__name__

    if fb_available:
        fb_platform = fallback["platform"]
        fb_model = fallback["model"]
        print(
            f"  WARNING: {member['label']} primary failed ({member['platform']}/{member['model']}), "
            f"trying fallback ({fb_platform}/{fb_model})...",
            file=sys.stderr,
        )
        try:
            review = _call_member(
                fb_platform, fb_model, fallback.get("api_key_env"),
                system_prompt, user_prompt,
                member["max_tokens"], member["temperature"],
                api_keys, timeout,
                review_mode=review_mode,
            )
            elapsed = time.monotonic() - start
            return role, review, elapsed
        except Exception as fb_err:
            fb_type = type(fb_err).__name__
            print(f"  [debug] {member['label']} fallback error: {fb_type}: {fb_err}", file=sys.stderr)
            opaque_msg = f"{primary_type} (primary) / {fb_type} (fallback)"
    else:
        opaque_msg = f"{primary_type}"

    elapsed = time.monotonic() - start
    placeholder = (
        f"### {role} Review: Sprint {sprint} (R{round_num})\n\n"
        f"**Status:** UNAVAILABLE\n"
        f"**Error:** ({opaque_msg})\n\n"
        f"This expert was unable to complete their review."
    )
    return role, placeholder, elapsed


def run_consolidator(
    config: dict, council_reviews: dict[str, str],
    member_labels: dict[str, str],
    sprint: str, title: str, round_num: int, review_type: str,
    api_keys: dict,
    tracker_content: str | None = None,
    escalation_note: str | None = None,
) -> str:
    """Run the consolidator to produce the final unified review."""
    consolidator = config["council"]["consolidator"]
    timeout = config["council"].get("consolidator_timeout_seconds", 180)
    retry_delay = config["council"].get("retry_delay_seconds", 5)

    system_prompt, user_prompt = build_consolidator_prompt(
        council_reviews, sprint, title, round_num, review_type, member_labels,
        tracker_content=tracker_content,
        escalation_note=escalation_note,
    )

    primary_err = None
    platform = consolidator["platform"]
    api_key_env = consolidator.get("api_key_env")
    api_key = None if platform == "codex" else api_keys.get(api_key_env, "")

    for attempt in range(2):
        try:
            return call_model(
                platform=platform,
                model=consolidator["model"],
                system=system_prompt,
                user_content=user_prompt,
                max_tokens=consolidator["max_tokens"],
                temperature=consolidator["temperature"],
                api_key=api_key,
                timeout=timeout,
            )
        except Exception as err:
            primary_err = err
            if attempt == 0:
                print(f"  [debug] Consolidator attempt 1 failed ({type(err).__name__}), retrying...", file=sys.stderr)
                time.sleep(retry_delay)
            else:
                print(f"  [debug] Consolidator attempt 2 failed: {err}", file=sys.stderr)

    fallback = consolidator.get("fallback")
    fb_key_env = fallback.get("api_key_env") if fallback else None
    fb_available = fallback and (fallback.get("platform") == "codex" or fb_key_env in api_keys)
    if fb_available:
        fb_platform = fallback["platform"]
        fb_model = fallback["model"]
        fb_api_key = None if fb_platform == "codex" else api_keys.get(fb_key_env, "")
        try:
            return call_model(
                platform=fb_platform, model=fb_model,
                system=system_prompt, user_content=user_prompt,
                max_tokens=consolidator["max_tokens"], temperature=consolidator["temperature"],
                api_key=fb_api_key, timeout=timeout,
            )
        except Exception as fb_err:
            print(f"  [debug] Consolidator fallback error: {type(fb_err).__name__}: {fb_err}", file=sys.stderr)

    print(f"  WARNING: Consolidator failed — using fallback consolidation", file=sys.stderr)
    return fallback_consolidation(council_reviews, sprint, title, round_num, review_type)


def fallback_consolidation(
    council_reviews: dict[str, str],
    sprint: str, title: str, round_num: int, review_type: str,
) -> str:
    """Produce a synthetic review from raw council outputs when consolidator fails."""
    review_type_cap = "Plan" if review_type == "plan" else "Code"
    has_high = any("[High]" in r for r in council_reviews.values())
    verdict = "CHANGES_REQUESTED" if has_high else "APPROVED"
    successful_count = sum(1 for r in council_reviews.values() if "UNAVAILABLE" not in r)
    all_reviews = "\n\n---\n\n".join(
        f"### {role.title()}\n{text}" for role, text in council_reviews.items()
    )
    return f"""## {review_type_cap} Review: Sprint {sprint} - {title} (R{round_num})

**Round:** {round_num}
**Verdict:** {verdict}
**Review Method:** Council of Experts ({successful_count} reviewers, consolidator FAILED — raw reviews below)

> Note: The consolidator was unable to synthesise these reviews. The verdict is a mechanical
> determination: CHANGES_REQUESTED if any [High] finding exists, else APPROVED.

{all_reviews}
"""


# ---------------------------------------------------------------------------
# Round Tracking
# ---------------------------------------------------------------------------


def increment_round(sprint: str, review_type: str, repo_root: Path) -> int:
    """Increment and return the review round number."""
    round_file = repo_root / f".review-round-sprint{sprint}-{review_type}"
    round_num = int(round_file.read_text().strip()) if round_file.exists() else 0
    round_num += 1
    round_file.write_text(str(round_num))

    if review_type == "plan" and round_num == 1:
        base_file = repo_root / f".sprint-base-commit-{sprint}"
        if not base_file.exists():
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                base_file.write_text(result.stdout.strip())

    return round_num


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def extract_verdict(review_text: str) -> str:
    """Extract the verdict line from review text."""
    for line in review_text.splitlines():
        if "**Verdict:**" in line:
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# Findings Tracker
# ---------------------------------------------------------------------------


# Canonical mapping from reviewer source labels to lens enum.
# Sourced from council-config.json member roles. Unknown -> "unknown".
LENS_MAP: dict[str, str] = {
    "security": "security",
    "security expert": "security",
    "code_quality": "code_quality",
    "code quality": "code_quality",
    "code quality expert": "code_quality",
    "test_quality": "test_quality",
    "test quality": "test_quality",
    "test quality expert": "test_quality",
    "domain": "domain",
    "domain expert": "domain",
}

import unicodedata as _unicodedata

_TAG_STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are", "and", "or"}

_TAG_SPLIT_RE = re.compile(r"[.:]")
_TAG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_LENS_SOURCE_RE = re.compile(r"\(Source:\s*([^)]+)\)")


def _derive_tag(title: str) -> str:
    """Deterministic tag derivation from a finding title.

    Algorithm:
      1. Split on first `.` or `:`; keep the head.
      2. Unicode-normalise (NFKD) and ASCII-fold (strip diacritics).
      3. Lowercase.
      4. Replace non-[a-z0-9] runs with `-`; strip leading/trailing `-`.
      5. Drop stopwords from token list.
      6. Take first 4 tokens; rejoin on `-`; truncate to 32 chars.
      7. Empty result -> `"untagged"`.

    Pure function; tested in tests/test_tag_derivation.py.
    """
    if not title:
        return "untagged"
    head = _TAG_SPLIT_RE.split(title, maxsplit=1)[0]
    head = _unicodedata.normalize("NFKD", head)
    head = head.encode("ascii", "ignore").decode("ascii")
    head = head.lower()
    head = _TAG_NON_ALNUM_RE.sub("-", head).strip("-")
    if not head:
        return "untagged"
    tokens = [t for t in head.split("-") if t and t not in _TAG_STOPWORDS]
    if not tokens:
        return "untagged"
    tag = "-".join(tokens[:4])[:32].rstrip("-")
    return tag or "untagged"


def _derive_lens(raw_line: str) -> str:
    """Extract lens from a `(Source: ...)` annotation, returning the
    first mapped lens. Falls back to "unknown"."""
    m = _LENS_SOURCE_RE.search(raw_line)
    if not m:
        return "unknown"
    sources = [s.strip().lower() for s in m.group(1).split(",")]
    for src in sources:
        if src in LENS_MAP:
            return LENS_MAP[src]
    return "unknown"


def _parse_findings(review_text: str, round_num: int) -> list[dict]:
    """Extract findings from consolidated review markdown."""
    findings = []
    finding_id = 0
    for line in review_text.splitlines():
        line_stripped = line.strip()
        if not (line_stripped.startswith("-") and "**[" in line_stripped):
            continue
        severity = None
        for sev in ("High", "Medium", "Low"):
            if f"[{sev}]" in line_stripped:
                severity = sev
                break
        if not severity:
            continue
        finding_id += 1
        desc = line_stripped
        marker = f"**[{severity}]**"
        idx = desc.find(marker)
        if idx >= 0:
            desc = desc[idx + len(marker):].strip().lstrip("-").strip()
        lens = _derive_lens(line_stripped)
        tag = _derive_tag(desc)
        if len(desc) > 120:
            desc = desc[:117] + "..."
        findings.append({
            "id": finding_id,
            "round": round_num,
            "severity": severity,
            "lens": lens,
            "tag": tag,
            "description": desc,
            "status": "OPEN",
            "resolution": "",
        })
    return findings


def _read_tracker(tracker_file: Path) -> list[dict]:
    """Parse existing tracker file into findings list.

    Backward-compatible: old 6-column trackers load with
    lens="unknown", tag="untagged". New 8-column trackers round-trip
    cleanly.
    """
    findings = []
    in_table = False
    header_cols: list[str] = []
    for line in tracker_file.read_text().splitlines():
        if line.startswith("| #"):
            in_table = True
            header_cols = [c.strip().lower() for c in line.split("|")[1:-1]]
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 6:
                fid = parts[0]
                col = {name: (parts[i] if i < len(parts) else "") for i, name in enumerate(header_cols)}
                findings.append({
                    "id": int(fid) if fid.isdigit() else 0,
                    "round": int(parts[1].lstrip("R")) if parts[1].lstrip("R").isdigit() else 0,
                    "severity": col.get("severity", parts[2] if len(parts) > 2 else ""),
                    "lens": col.get("lens", "unknown") or "unknown",
                    "tag": col.get("tag", "untagged") or "untagged",
                    "description": col.get("finding", parts[3] if len(parts) > 3 else ""),
                    "status": col.get("status", parts[4] if len(parts) > 4 else "OPEN"),
                    "resolution": col.get("resolution", parts[5] if len(parts) > 5 else ""),
                    "routed": _parse_routed_column(col.get("routed", "")),
                })
        elif in_table and not line.startswith("|"):
            in_table = False
    return findings


def _parse_routed_column(raw: str) -> list[int]:
    """Parse the ``Routed`` column (Sprint 6 schema v3). Values have
    shape ``R1,R3`` on disk and load as ``[1, 3]``. Absent column
    (v2 tracker) yields ``[]`` — a conservative "we don't know"
    default. Malformed tokens are skipped with a stderr notice rather
    than crashing the whole load."""
    if not raw:
        return []
    out: list[int] = []
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        try:
            if t.startswith(("R", "r")):
                out.append(int(t[1:]))
            else:
                out.append(int(t))
        except ValueError:
            print(
                f"tracker: skipping malformed Routed token {t!r}",
                file=sys.stderr,
            )
    return out


def _format_routed_column(routed: list[int]) -> str:
    if not routed:
        return ""
    return ",".join(f"R{n}" for n in sorted(set(routed)))


def _text_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity (Jaccard)."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _merge_findings(
    existing: list[dict],
    new_findings: list[dict],
    round_num: int,
    *,
    routed_lenses: set[str] | None = None,
) -> list[dict]:
    """Merge new findings with existing tracker.

    Sprint 6 routing: when ``routed_lenses`` is provided, findings whose
    ``lens`` is NOT in the set are left completely untouched — no
    status transition, no round-number update, no oscillation-counter
    bump. This is the enforcement point that makes "skipped lens's
    prior-round OPEN findings carry forward" correct without the
    consolidator needing to interpret silence. ``routed_lenses=None``
    preserves pre-Sprint-6 behaviour (every lens is considered routed).

    Oscillation detection: if a finding is reopened for the 3rd time
    (ADDRESSED → re-raised 3+ times), it is auto-marked RECURRING and
    removed from blocking status. Prevents infinite review loops.

    All findings (new and existing) get their ``routed`` list updated
    with ``round_num`` when their lens is in ``routed_lenses``. This
    column is pure audit metadata — useful for post-hoc "which rounds
    ran which lenses" analysis.
    """
    merged = list(existing)
    next_id = max((f["id"] for f in merged), default=0) + 1

    def _lens_routed(lens: str) -> bool:
        return routed_lenses is None or lens in routed_lenses

    for ef in merged:
        ef.setdefault("routed", [])
        if _lens_routed(ef.get("lens", "unknown")) and round_num not in ef["routed"]:
            ef["routed"] = sorted(set(ef["routed"]) | {round_num})

    for nf in new_findings:
        nf_lens = nf.get("lens") or "unknown"
        if not _lens_routed(nf_lens):
            # Defensive: a lens that wasn't routed shouldn't have emitted
            # findings at all. Drop them rather than polluting the tracker.
            continue
        matched = False
        for ef in merged:
            if not _lens_routed(ef.get("lens", "unknown")):
                continue
            if (ef["severity"] == nf["severity"]
                    and _text_similarity(ef["description"], nf["description"]) > 0.4):
                matched = True
                if ef["status"] in ("ADDRESSED", "REOPENED"):
                    # Count how many times this finding has been reopened
                    reopen_count = ef["resolution"].count("Reopened")
                    if reopen_count >= 2:
                        # 3rd reopen — mark as RECURRING (oscillating)
                        ef["status"] = "RECURRING"
                        ef["resolution"] += f" [Oscillating — auto-demoted to Known Debt at R{round_num}]"
                    else:
                        ef["status"] = "REOPENED"
                        ef["resolution"] += f" [Reopened R{round_num}]"
                # Skip RECURRING findings — they stay as Known Debt
                break
        if not matched:
            nf["id"] = next_id
            nf["round"] = round_num
            nf["routed"] = [round_num]
            next_id += 1
            merged.append(nf)

    return merged


def _write_tracker(tracker_file: Path, sprint: str, findings: list[dict], review_type: str) -> None:
    """Write findings tracker as markdown table.

    Schema v3 (Sprint 6, 9 columns): adds ``Routed`` after Resolution
    as per-round audit metadata. Format: comma-separated ``R<int>``
    tokens (e.g. ``R1,R3``). Empty list writes as empty string.
    v2 trackers load with ``routed=[]`` and are upgraded in place on
    the first rewrite.
    """
    lines = [
        f"# Findings Tracker: Sprint {sprint} ({review_type})",
        "",
        "Editor: Update the **Status** and **Resolution** columns after addressing each finding.",
        "Status values: `OPEN` | `ADDRESSED` | `VERIFIED` | `WONTFIX` | `REOPENED`",
        "",
        "| # | Round | Severity | Lens | Tag | Finding | Status | Resolution | Routed |",
        "|---|-------|----------|------|-----|---------|--------|------------|--------|",
    ]
    for f in findings:
        lens = f.get("lens") or "unknown"
        tag = f.get("tag") or "untagged"
        routed = _format_routed_column(f.get("routed") or [])
        lines.append(
            f"| {f['id']} | R{f['round']} | {f['severity']} | {lens} | {tag} "
            f"| {f['description']} | {f['status']} | {f['resolution']} | {routed} |"
        )
    lines.append("")
    tracker_file.write_text("\n".join(lines))


def update_findings_tracker(
    sprint: str, round_num: int, review_text: str,
    review_type: str, repo_root: Path,
    *,
    routed_lenses: set[str] | None = None,
) -> Path:
    """Parse findings from consolidated review and update the tracker file.

    Sprint 6: ``routed_lenses`` propagates into ``_merge_findings`` so
    a skipped lens's prior findings carry forward untouched.
    """
    tracker_file = repo_root / f"FINDINGS_Sprint{sprint}.md"
    new_findings = _parse_findings(review_text, round_num)

    if not tracker_file.exists():
        # First round: stamp every finding with the round it was routed in.
        for nf in new_findings:
            nf["routed"] = [round_num]
        _write_tracker(tracker_file, sprint, new_findings, review_type)
    else:
        existing = _read_tracker(tracker_file)
        merged = _merge_findings(
            existing, new_findings, round_num,
            routed_lenses=routed_lenses,
        )
        _write_tracker(tracker_file, sprint, merged, review_type)

    return tracker_file


def compute_convergence_score(tracker_file: Path) -> tuple[float, str]:
    """Compute convergence score from tracker.

    Sprint 5: surfaces RECURRING counts alongside resolved/open/reopened
    so that the single-line convergence summary conveys oscillation
    state without needing a separate print. The RECURRING clause is
    appended only when the count is non-zero, keeping healthy-sprint
    output terse.
    """
    if not tracker_file.exists():
        return 0.0, "No tracker"
    findings = _read_tracker(tracker_file)
    if not findings:
        return 1.0, "No findings"
    total = len(findings)
    resolved = sum(1 for f in findings if f["status"] in ("ADDRESSED", "VERIFIED", "WONTFIX"))
    open_count = sum(1 for f in findings if f["status"] == "OPEN")
    reopened = sum(1 for f in findings if f["status"] == "REOPENED")
    recurring = sum(1 for f in findings if f["status"] == "RECURRING")
    score = resolved / total if total > 0 else 1.0
    desc = f"{resolved}/{total} resolved, {open_count} open, {reopened} reopened"
    if recurring:
        desc += f", {recurring} recurring"
    return score, desc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


_SPRINT_RE = re.compile(r"^\d+$")


def _numeric_sprint(raw: str) -> str:
    """Validate the sprint CLI argument. Sprint 6 R1 #15: the sprint
    is interpolated into repo-relative paths (tracker, base-commit,
    metrics JSONL); a crafted non-numeric value such as ``../../etc``
    would escape the repo root. We accept digits only."""
    if not _SPRINT_RE.match(raw):
        raise argparse.ArgumentTypeError(
            f"sprint must be numeric (got {raw!r})"
        )
    return raw


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="council-review.py",
        description="Council of Experts review driver (plan or code).",
    )
    parser.add_argument(
        "review_type", choices=["plan", "code"],
        help="Review an implementation plan or the code changes.",
    )
    parser.add_argument(
        "sprint", type=_numeric_sprint,
        help="Sprint number (digits only, e.g. 2).",
    )
    parser.add_argument(
        "title", nargs="+",
        help="Sprint title (quoted or bare tokens).",
    )
    parser.add_argument(
        "--allow-untracked", action="store_true",
        help="(code review only) include untracked source files with a banner.",
    )
    parser.add_argument(
        "--lenses", default=None,
        help="(code review only) comma-separated subset of lens roles "
             "to run (e.g. 'security,code_quality'). Rejected on plan "
             "reviews. See council-config.json for valid roles.",
    )
    parser.add_argument(
        "--auto-lenses", action="store_true",
        help="(code review only) auto-select lenses based on the diff: "
             "security + code_quality always; test_quality when tests/ "
             "changed; domain when knowledge/ changed. Overridden by "
             "--lenses.",
    )
    parser.add_argument(
        "--allow-no-security", action="store_true",
        help="(code review only) explicitly acknowledge skipping the "
             "security lens. Required when --lenses omits security. "
             "Recorded as security_bypassed=true in metrics.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show per-member timing and the full header block. "
             "Default console output is terse.",
    )
    return parser.parse_args(argv)


def _resolve_routed_lenses(
    args: argparse.Namespace,
    config: dict,
    changed_paths: list[str],
) -> tuple[set[str] | None, bool]:
    """Resolve the routing decision from CLI args + config + diff.

    Returns ``(routed_lenses, security_bypassed)``. A ``None`` first
    value means "no routing — run every active member". The security
    bypass flag records whether the caller opted out of the security
    lens.

    Plan reviews always return ``(None, False)`` — the plan has no
    per-lens file signature, and the routing flags are rejected at
    argparse level.
    """
    if args.review_type == "plan":
        if args.lenses or args.auto_lenses or args.allow_no_security:
            raise LensArgError(
                "--lenses / --auto-lenses / --allow-no-security are "
                "only valid for code reviews"
            )
        return None, False

    valid = _known_lens_roles(config)
    lenses: set[str] | None = None
    if args.lenses is not None:
        lenses = parse_lenses_arg(args.lenses, valid)
    elif args.auto_lenses:
        lenses = auto_lens_set(changed_paths, valid)

    if lenses is None:
        return None, False

    lenses, bypassed = enforce_security_lens(
        lenses,
        allow_no_security=args.allow_no_security,
        review_type=args.review_type,
    )
    return lenses, bypassed


def _apply_forced_verdict(
    consolidated: str,
    tracker_file: Path,
    review_output_file: Path,
    round_num: int,
    max_rounds: int,
    verdict: str | None,
) -> str | None:
    """Force an APPROVED verdict when max rounds is exceeded and no
    genuinely-new [High] findings were raised this round. Returns the
    updated verdict string, or None when no override was applied.
    Sprint 6: extracted from main() to keep the orchestrator readable."""
    if round_num <= max_rounds or not verdict or "APPROVED" in verdict:
        return None
    updated_findings = _read_tracker(tracker_file)
    new_high_this_round = [
        f for f in updated_findings
        if f["round"] == round_num
        and f["severity"] == "High"
        and f["status"] == "OPEN"
    ]
    if new_high_this_round:
        print()
        print(f"  WARNING: {len(new_high_this_round)} new [High] finding(s) at round {round_num} despite exceeding max rounds.")
        print(f"  These are genuinely new concerns. The editor should address them or escalate to the human.")
        return None
    print()
    print(f"  FORCED VERDICT: No new [High] findings at round {round_num} (past max {max_rounds}).")
    print(f"  Overriding consolidator verdict to APPROVED with Known Debt.")
    forced = re.sub(
        r"(\*\*Verdict:\*\*\s*).*",
        r"\1APPROVED (forced — max rounds exceeded, no new [High] findings)",
        consolidated, count=1,
    )
    forced = re.sub(
        r"(## Verdict:\s*).*",
        r"\1APPROVED (forced — max rounds exceeded, no new [High] findings)",
        forced, count=1,
    )
    if "## Known Debt" not in forced:
        open_items = [f for f in updated_findings if f["status"] in ("OPEN", "REOPENED", "RECURRING")]
        if open_items:
            debt_lines = ["\n\n## Known Debt\n",
                          "The following items remain unresolved but are accepted as known debt:\n"]
            for item in open_items:
                debt_lines.append(f"- [{item['severity']}] {item['description']} (from R{item['round']}, status: {item['status']})")
            forced += "\n".join(debt_lines) + "\n"
    review_output_file.write_text(forced)
    new_verdict = "APPROVED (forced — max rounds exceeded, no new [High] findings)"
    print(f"    Updated verdict: {new_verdict}")
    return new_verdict


def _prepare_council_dir(repo_root: Path, config: dict) -> Path:
    """Resolve + recreate the council output directory. Bounds-checked
    to stay inside the repo root (defence against a malicious
    ``output_dir`` in the config file)."""
    output_dir_value = config["council"].get("output_dir", "council")
    council_dir = (repo_root / output_dir_value).resolve()
    repo_root_resolved = repo_root.resolve()
    if not str(council_dir).startswith(str(repo_root_resolved) + os.sep):
        print(
            "ERROR: council.output_dir resolves outside repo root. Refusing.",
            file=sys.stderr,
        )
        sys.exit(1)
    if council_dir.exists():
        shutil.rmtree(council_dir)
    council_dir.mkdir(parents=True)
    return council_dir


def _run_parallel_council(
    active_members: list[dict],
    materials: str,
    api_keys: dict[str, str],
    sprint: str,
    title: str,
    round_num: int,
    review_type: str,
    tracker_content: str | None,
    council_dir: Path,
    config: dict,
    *,
    verbose: bool,
) -> tuple[dict[str, str], int]:
    """Fan out to every active member, collect their reviews, write
    each to ``council_dir/<role>.md``, return (council_reviews,
    successful_count). Sprint 6: extracted from main() to control
    complexity (R1 #20)."""
    codex_stagger = config["council"].get("codex_stagger_seconds", 2)
    retry_delay = config["council"].get("retry_delay_seconds", 5)
    parallel_timeout = config["council"].get("parallel_timeout_seconds", 180)

    print(f"  Running {len(active_members)} council members in parallel...")
    council_reviews: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(active_members)) as executor:
        futures = {
            executor.submit(
                run_council_member,
                member, materials, api_keys,
                sprint, title, round_num, review_type,
                parallel_timeout,
                codex_stagger=codex_stagger,
                retry_delay=retry_delay,
                tracker_content=tracker_content,
            ): member
            for member in active_members
        }
        for future in as_completed(futures):
            member = futures[future]
            try:
                role, review_text, elapsed = future.result(timeout=parallel_timeout + 30)
                council_reviews[role] = review_text
                (council_dir / f"{role}.md").write_text(review_text)
                status = "UNAVAILABLE" if "UNAVAILABLE" in review_text else "done"
                if verbose or status == "UNAVAILABLE":
                    print(f"    {member['label']:25s} {status:12s} ({elapsed:.1f}s)")
            except Exception as e:  # noqa: BLE001
                role = member["role"]
                print(
                    f"  [debug] {member['label']} future error: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                council_reviews[role] = (
                    f"### {role} Review: Sprint {sprint} (R{round_num})\n\n"
                    f"**Status:** UNAVAILABLE\n"
                    f"**Error:** ({type(e).__name__})\n\n"
                    f"This expert was unable to complete their review."
                )
                print(f"    {member['label']:25s} FAILED       ({type(e).__name__})")

    successful = sum(1 for r in council_reviews.values() if "UNAVAILABLE" not in r)
    print()
    print(f"  Council complete: {successful}/{len(active_members)} experts succeeded")
    return council_reviews, successful


def _print_header(
    review_type: str, sprint: str, round_num: int, title: str,
    active_members: list[dict], consolidator: dict, *, verbose: bool,
) -> None:
    """Sprint 6 R1 #20: terse 3-line header by default; full detail
    under --verbose."""
    lens_names = ",".join(m["role"] for m in active_members)
    print(f"==> Council {review_type} review: Sprint {sprint} R{round_num} ({title})")
    print(f"    Lenses: {len(active_members)} ({lens_names}) + consolidator")
    if verbose:
        print(f"    Review type:    {review_type}")
        for m in active_members:
            print(f"      - {m['label']:25s} ({m['platform']}/{m['model']})")
        print(f"    Consolidator:   {consolidator['platform']}/{consolidator['model']}")
    print()


def _gather_materials(
    review_type: str, sprint: str, repo_root: Path,
    *,
    preflight_banner: str, allow_untracked: bool,
) -> str:
    """Gather + redact materials for the chosen review_type."""
    print("  Gathering materials...")
    if review_type == "plan":
        materials = gather_plan_materials(sprint, repo_root)
    else:
        materials = gather_code_materials(
            sprint, repo_root,
            banner=preflight_banner,
            include_untracked=allow_untracked,
        )
    print(f"  Materials: {len(materials):,} chars")
    materials = redact_secrets(materials)
    print()
    return materials


def _print_next_steps(
    verdict: str | None, review_type: str, sprint: str, title: str,
    round_num: int, max_rounds: int, repo_root: Path,
) -> None:
    """Print the "Next:" block + optional compaction hint."""
    print()
    if verdict and "APPROVED" in verdict and "CHANGES_REQUESTED" not in verdict:
        try:
            sys.path.insert(0, str(repo_root / "scripts"))
            from profile import is_enabled as _is_enabled  # type: ignore
            if _is_enabled("compaction", repo_root):
                print(
                    "  → Milestone reached. Consider running /compact before continuing.",
                    file=sys.stderr,
                )
        except Exception:
            pass
        if review_type == "plan":
            print("  Next: Proceed to implementation (Phase 2)")
        else:
            print(f'  Next: ./scripts/archive-plan.sh {sprint} "{title}"')
        return
    remaining = max_rounds - round_num
    if remaining > 0:
        print(f"  Next: Address findings in FINDINGS_Sprint{sprint}.md, then re-run:")
        print(f'        ./scripts/council-review.py {review_type} {sprint} "{title}"')
        print(f"        ({remaining} round(s) remaining before forced approval)")
    else:
        print("  ESCALATION: Max rounds reached. Present unresolved findings to the human.")
        print("  Options: cut scope, override with higher max_rounds, or accept Known Debt.")


def _safe_emit_metrics(
    repo_root: Path, sprint: str, review_type: str, round_num: int,
    *,
    active_members: list[dict],
    council_reviews: dict[str, str],
    elapsed_s: float,
    verdict: str | None,
    tracker_file: Path,
    security_bypassed: bool,
) -> None:
    """Wrapper around _emit_metrics that never raises into main()."""
    try:
        _emit_metrics(
            repo_root, sprint, review_type, round_num,
            members_active=len(active_members),
            members_succeeded=sum(
                1 for txt in council_reviews.values() if "UNAVAILABLE" not in txt
            ),
            elapsed_s=elapsed_s,
            verdict=verdict,
            tracker_file=tracker_file,
            security_bypassed=security_bypassed,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] metrics emit failed: {exc}", file=sys.stderr)


def main():
    global _codex_call_index, _codex_call_lock

    ns = _parse_args(sys.argv[1:])
    review_type = ns.review_type
    sprint = ns.sprint
    title = " ".join(ns.title)

    repo_root = Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip() or ".")

    preflight_banner = ""
    if review_type == "code":
        pf = preflight_code_review(repo_root, ns.allow_untracked)
        if not pf.ok:
            print(pf.reject_message, file=sys.stderr)
            sys.exit(4)
        preflight_banner = pf.banner

    ensure_api_keys_from_profile()

    config_path = repo_root / "scripts" / "council-config.json"
    config = load_config(config_path)

    # Sprint 6: resolve selective routing. Compute changed paths for
    # --auto-lenses before we filter active members.
    changed_paths: list[str] = []
    if review_type == "code" and ns.auto_lenses:
        changed_paths = get_changed_files(sprint=sprint, repo_root=repo_root)
    try:
        routed_lenses, security_bypassed = _resolve_routed_lenses(
            ns, config, changed_paths
        )
    except LensArgError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    active_members = get_active_members(config, review_type, lenses=routed_lenses)
    if not active_members:
        print(f"ERROR: No council members configured for '{review_type}' phase", file=sys.stderr)
        sys.exit(1)

    api_keys = validate_api_keys(config, active_members)
    round_num = increment_round(sprint, review_type, repo_root)

    _print_header(
        review_type, sprint, round_num, title,
        active_members, config["council"]["consolidator"],
        verbose=ns.verbose,
    )

    materials = _gather_materials(
        review_type, sprint, repo_root,
        preflight_banner=preflight_banner,
        allow_untracked=ns.allow_untracked,
    )

    # Read findings tracker (needed by both council members and consolidator)
    tracker_file = repo_root / f"FINDINGS_Sprint{sprint}.md"
    tracker_content = tracker_file.read_text() if tracker_file.exists() else None

    council_dir = _prepare_council_dir(repo_root, config)
    member_labels = {m["role"]: m["label"] for m in active_members}

    import threading
    _codex_call_index = 0
    _codex_call_lock = threading.Lock()

    council_reviews, successful = _run_parallel_council(
        active_members, materials, api_keys,
        sprint, title, round_num, review_type,
        tracker_content, council_dir, config,
        verbose=ns.verbose,
    )

    if successful < QUORUM_THRESHOLD:
        print(f"  ERROR: Quorum not met ({successful} < {QUORUM_THRESHOLD}). Aborting.", file=sys.stderr)
        sys.exit(1)

    max_rounds_key = "max_plan_rounds" if review_type == "plan" else "max_code_rounds"
    max_rounds = config["council"].get(max_rounds_key, 8)
    warning_at = config["council"].get("convergence_warning_at", 3)

    escalation_note = None
    if round_num > max_rounds:
        escalation_note = (
            f"\nESCALATION: This is round {round_num}, exceeding the configured maximum "
            f"of {max_rounds}. You MUST:\n"
            f"1. Only flag genuinely NEW [High] findings not present in prior rounds\n"
            f"2. If no new [High] findings exist, verdict MUST be APPROVED\n"
            f"3. List all unresolved items in a 'Known Debt' section instead of blocking\n"
        )

    print(f"  Running consolidator...")
    start = time.monotonic()
    consolidated = run_consolidator(
        config, council_reviews, member_labels,
        sprint, title, round_num, review_type, api_keys,
        tracker_content=tracker_content,
        escalation_note=escalation_note,
    )
    elapsed = time.monotonic() - start
    print(f"  Consolidator complete ({elapsed:.1f}s)")

    review_output_file = repo_root / f"REVIEW_Sprint{sprint}.md"
    review_output_file.write_text(consolidated)
    print()
    print(f"==> Review written to {review_output_file.name}")

    tracker_file = update_findings_tracker(
        sprint, round_num, consolidated, review_type, repo_root,
        routed_lenses=routed_lenses,
    )
    print(f"    Findings tracker: {tracker_file.name}")

    verdict = extract_verdict(consolidated)
    if verdict:
        print(f"    {verdict}")
    else:
        print("    WARNING: No verdict found in consolidated review")

    # ----- Forced verdict logic: override consolidator after max rounds -----
    forced = _apply_forced_verdict(
        consolidated, tracker_file, review_output_file,
        round_num, max_rounds, verdict,
    )
    if forced is not None:
        verdict = forced

    # ----- Convergence reporting -----
    # Sprint 5: the recurring count is embedded in `desc` by
    # compute_convergence_score; the prior standalone "RECURRING: ..."
    # print has been removed as redundant.
    if round_num > 1:
        score, desc = compute_convergence_score(tracker_file)
        print(f"    Convergence: {score:.0%} ({desc})")
        if score < 0.5 and round_num >= warning_at:
            print(f"    WARNING: Low convergence at round {round_num}. Consider addressing [High] items only.")

    _safe_emit_metrics(
        repo_root, sprint, review_type, round_num,
        active_members=active_members,
        council_reviews=council_reviews,
        elapsed_s=elapsed,
        verdict=verdict,
        tracker_file=tracker_file,
        security_bypassed=security_bypassed,
    )
    _print_next_steps(
        verdict, review_type, sprint, title,
        round_num, max_rounds, repo_root,
    )


if __name__ == "__main__":
    main()

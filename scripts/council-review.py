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

Last updated: Sprint 5 (2026-04-16) -- call_codex tempfile removed; RECURRING surfaced in convergence reporting

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
) -> None:
    """Append per-round metrics to council/metrics_Sprint<N>.json (Sprint 127 v6).

    One JSON line per round so sprint cost can be compared post-hoc.
    Safe to call repeatedly; appends to an existing file.
    """
    findings: list[dict] = []
    if tracker_file.exists():
        try:
            findings = _read_tracker(tracker_file)
        except Exception:  # noqa: BLE001
            findings = []

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
        "verdict": verdict or "UNKNOWN",
    }

    out_dir = repo_root / "council"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"metrics_Sprint{sprint}.jsonl"
    with out_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


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
            value = value.strip().strip('"').strip("'")
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


def get_active_members(config: dict, review_type: str) -> list[dict]:
    """Return only council members whose phases include the review type."""
    return [
        m for m in config["council"]["members"]
        if review_type in m.get("phases", ["plan", "code"])
    ]


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
        cmd = ["claude", "-p", "--model", model, "--permission-mode", "bypassPermissions"]
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


def build_council_prompt(
    member: dict, materials: str,
    sprint: str, title: str, round_num: int, review_type: str,
    tracker_content: str | None = None,
) -> tuple[str, str]:
    """Build system + user prompts for a council member. Returns (system, user)."""
    role = member["role"]
    label = member["label"]
    lens = member["lens"]

    if round_num == 1:
        round_context = "This is the first review of this plan."
    else:
        round_context = (
            f"This is round {round_num}. The artifact has been revised to address "
            f"findings from previous rounds.\n\n"
            f"FOCUS on:\n"
            f"1. Whether previous findings have been adequately addressed\n"
            f"2. Any genuinely NEW issues introduced by the revisions\n\n"
            f"Do NOT re-raise findings that have been marked ADDRESSED in the tracker "
            f"unless the fix is demonstrably incomplete. Do NOT introduce novel concerns "
            f"about previously-reviewed sections that haven't changed."
        )

    tracker_section = ""
    if tracker_content and round_num > 1:
        tracker_section = f"""

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

    review_type_label = "plan" if review_type == "plan" else "code implementation"

    system_prompt = f"""You are {label} on a review council for a pair programming workflow.

## Your Review Lens
{lens}"""

    user_prompt = f"""## Review Type
This is a {review_type_label} review for Sprint {sprint}: {title} (Round {round_num}).
{round_context}
{tracker_section}
## Materials Under Review
{materials}

## Output Format

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

    system_prompt = """You are the Consolidation Lead for a review council. Multiple domain experts have independently reviewed a plan or implementation. Your job is to synthesise their findings into a single, coherent review with one verdict."""

    if review_type == "plan":
        assessment_sections = """### Design Assessment
[Synthesised evaluation of the proposed approach]

### Completeness
[Does the plan cover all deliverables and edge cases?]"""
    else:
        assessment_sections = """### Implementation Assessment
[Does the code correctly implement the approved plan?]

### Code Quality
[Synthesised assessment of clarity, documentation, error handling]

### Test Coverage
[Synthesised assessment of test adequacy]"""

    tracker_section = ""
    if tracker_content and round_num > 1:
        tracker_section = f"""

## Prior Findings Tracker
Items marked ADDRESSED have been fixed. Do NOT re-flag ADDRESSED items unless the fix is demonstrably incomplete.

{tracker_content}
"""

    escalation_section = ""
    if escalation_note:
        escalation_section = f"\n{escalation_note}\n"

    user_prompt = f"""## Council Reviews

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
**Verdict:** APPROVED | CHANGES_REQUESTED{" | PLAN_REVISION_REQUIRED" if review_type == "code" else ""}
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

{"### Plan Revisions (if PLAN_REVISION_REQUIRED)" + chr(10) + "[What needs to change in the plan]" + chr(10) if review_type == "code" else ""}
### Recommendations
[Consolidated optional improvements]

### Expert Concordance
| Area | Experts Agreeing | Key Theme |
|------|-----------------|-----------|
| ... | ... | ... |"""

    return system_prompt, user_prompt


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
                })
        elif in_table and not line.startswith("|"):
            in_table = False
    return findings


def _text_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity (Jaccard)."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _merge_findings(existing: list[dict], new_findings: list[dict], round_num: int) -> list[dict]:
    """Merge new findings with existing tracker.

    Oscillation detection: if a finding is reopened for the 3rd time (i.e., it has
    been ADDRESSED and then re-raised 3+ times), it is auto-marked RECURRING and
    removed from blocking status. This prevents infinite review loops from findings
    that oscillate between fix attempts.
    """
    merged = list(existing)
    next_id = max((f["id"] for f in merged), default=0) + 1

    for nf in new_findings:
        matched = False
        for ef in merged:
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
            next_id += 1
            merged.append(nf)

    return merged


def _write_tracker(tracker_file: Path, sprint: str, findings: list[dict], review_type: str) -> None:
    """Write findings tracker as markdown table.

    Schema v2 (8 columns): adds Lens and Tag after Severity. Old
    trackers loaded via _read_tracker gain default lens/tag values.
    """
    lines = [
        f"# Findings Tracker: Sprint {sprint} ({review_type})",
        "",
        "Editor: Update the **Status** and **Resolution** columns after addressing each finding.",
        "Status values: `OPEN` | `ADDRESSED` | `VERIFIED` | `WONTFIX` | `REOPENED`",
        "",
        "| # | Round | Severity | Lens | Tag | Finding | Status | Resolution |",
        "|---|-------|----------|------|-----|---------|--------|------------|",
    ]
    for f in findings:
        lens = f.get("lens") or "unknown"
        tag = f.get("tag") or "untagged"
        lines.append(
            f"| {f['id']} | R{f['round']} | {f['severity']} | {lens} | {tag} "
            f"| {f['description']} | {f['status']} | {f['resolution']} |"
        )
    lines.append("")
    tracker_file.write_text("\n".join(lines))


def update_findings_tracker(
    sprint: str, round_num: int, review_text: str,
    review_type: str, repo_root: Path,
) -> Path:
    """Parse findings from consolidated review and update the tracker file."""
    tracker_file = repo_root / f"FINDINGS_Sprint{sprint}.md"
    new_findings = _parse_findings(review_text, round_num)

    if not tracker_file.exists():
        _write_tracker(tracker_file, sprint, new_findings, review_type)
    else:
        existing = _read_tracker(tracker_file)
        merged = _merge_findings(existing, new_findings, round_num)
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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="council-review.py",
        description="Council of Experts review driver (plan or code).",
    )
    parser.add_argument(
        "review_type", choices=["plan", "code"],
        help="Review an implementation plan or the code changes.",
    )
    parser.add_argument("sprint", help="Sprint number (e.g. 2).")
    parser.add_argument(
        "title", nargs="+",
        help="Sprint title (quoted or bare tokens).",
    )
    parser.add_argument(
        "--allow-untracked", action="store_true",
        help="(code review only) include untracked source files with a banner.",
    )
    return parser.parse_args(argv)


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

    active_members = get_active_members(config, review_type)
    if not active_members:
        print(f"ERROR: No council members configured for '{review_type}' phase", file=sys.stderr)
        sys.exit(1)

    api_keys = validate_api_keys(config, active_members)
    round_num = increment_round(sprint, review_type, repo_root)

    # Print header
    consolidator = config["council"]["consolidator"]
    print(f"==> Council review for Sprint {sprint}: {title} (Round {round_num})")
    print(f"    Review type:    {review_type}")
    print(f"    Active members: {len(active_members)}")
    for m in active_members:
        print(f"      - {m['label']:25s} ({m['platform']}/{m['model']})")
    print(f"    Consolidator:   {consolidator['platform']}/{consolidator['model']}")
    print()

    # Gather materials
    print("  Gathering materials...")
    if review_type == "plan":
        materials = gather_plan_materials(sprint, repo_root)
    else:
        materials = gather_code_materials(
            sprint, repo_root,
            banner=preflight_banner,
            include_untracked=ns.allow_untracked,
        )
    print(f"  Materials: {len(materials):,} chars")

    # Redact secrets before sending externally
    materials = redact_secrets(materials)
    print()

    # Read findings tracker (needed by both council members and consolidator)
    tracker_file = repo_root / f"FINDINGS_Sprint{sprint}.md"
    tracker_content = tracker_file.read_text() if tracker_file.exists() else None

    # Prepare council output directory
    output_dir_value = config["council"].get("output_dir", "council")
    council_dir = (repo_root / output_dir_value).resolve()
    repo_root_resolved = repo_root.resolve()
    if not str(council_dir).startswith(str(repo_root_resolved) + os.sep):
        print(f"ERROR: council.output_dir resolves outside repo root. Refusing.", file=sys.stderr)
        sys.exit(1)
    if council_dir.exists():
        shutil.rmtree(council_dir)
    council_dir.mkdir(parents=True)

    member_labels = {m["role"]: m["label"] for m in active_members}

    codex_stagger = config["council"].get("codex_stagger_seconds", 2)
    retry_delay = config["council"].get("retry_delay_seconds", 5)

    import threading
    _codex_call_index = 0
    _codex_call_lock = threading.Lock()

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
                review_file = council_dir / f"{role}.md"
                review_file.write_text(review_text)
                status = "UNAVAILABLE" if "UNAVAILABLE" in review_text else "done"
                print(f"    {member['label']:25s} {status:12s} ({elapsed:.1f}s)")
            except Exception as e:
                role = member["role"]
                print(f"  [debug] {member['label']} future error: {type(e).__name__}: {e}", file=sys.stderr)
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

    tracker_file = update_findings_tracker(sprint, round_num, consolidated, review_type, repo_root)
    print(f"    Findings tracker: {tracker_file.name}")

    verdict = extract_verdict(consolidated)
    if verdict:
        print(f"    {verdict}")
    else:
        print("    WARNING: No verdict found in consolidated review")

    # ----- Forced verdict logic: override consolidator after max rounds -----
    if round_num > max_rounds and verdict and "APPROVED" not in verdict:
        # Check if there are genuinely new [High] findings in this round
        updated_findings = _read_tracker(tracker_file)
        new_high_this_round = [
            f for f in updated_findings
            if f["round"] == round_num
            and f["severity"] == "High"
            and f["status"] == "OPEN"
        ]
        if not new_high_this_round:
            # No new [High] findings — force APPROVED with Known Debt
            print()
            print(f"  FORCED VERDICT: No new [High] findings at round {round_num} (past max {max_rounds}).")
            print(f"  Overriding consolidator verdict to APPROVED with Known Debt.")

            # Rewrite the verdict in the review file
            consolidated_forced = re.sub(
                r"(\*\*Verdict:\*\*\s*).*",
                r"\1APPROVED (forced — max rounds exceeded, no new [High] findings)",
                consolidated,
                count=1,
            )
            # Also rewrite ## Verdict: line if present
            consolidated_forced = re.sub(
                r"(## Verdict:\s*).*",
                r"\1APPROVED (forced — max rounds exceeded, no new [High] findings)",
                consolidated_forced,
                count=1,
            )

            # Append Known Debt section if not already present
            if "## Known Debt" not in consolidated_forced:
                open_items = [f for f in updated_findings if f["status"] in ("OPEN", "REOPENED", "RECURRING")]
                if open_items:
                    debt_lines = ["\n\n## Known Debt\n",
                                  "The following items remain unresolved but are accepted as known debt:\n"]
                    for item in open_items:
                        debt_lines.append(f"- [{item['severity']}] {item['description']} (from R{item['round']}, status: {item['status']})")
                    consolidated_forced += "\n".join(debt_lines) + "\n"

            review_output_file.write_text(consolidated_forced)
            verdict = "APPROVED (forced — max rounds exceeded, no new [High] findings)"
            print(f"    Updated verdict: {verdict}")
        else:
            print()
            print(f"  WARNING: {len(new_high_this_round)} new [High] finding(s) at round {round_num} despite exceeding max rounds.")
            print(f"  These are genuinely new concerns. The editor should address them or escalate to the human.")

    # ----- Convergence reporting -----
    # Sprint 5: the recurring count is embedded in `desc` by
    # compute_convergence_score; the prior standalone "RECURRING: ..."
    # print has been removed as redundant.
    if round_num > 1:
        score, desc = compute_convergence_score(tracker_file)
        print(f"    Convergence: {score:.0%} ({desc})")
        if score < 0.5 and round_num >= warning_at:
            print(f"    WARNING: Low convergence at round {round_num}. Consider addressing [High] items only.")

    # ----- Sprint 127 v6: emit per-round metrics for cost comparison -----
    try:
        _emit_metrics(
            repo_root, sprint, review_type, round_num,
            members_active=len(active_members),
            members_succeeded=sum(
                1 for txt in council_reviews.values() if "UNAVAILABLE" not in txt
            ),
            elapsed_s=elapsed,
            verdict=verdict,
            tracker_file=tracker_file,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] metrics emit failed: {exc}", file=sys.stderr)

    print()
    if verdict and "APPROVED" in verdict and "CHANGES_REQUESTED" not in verdict:
        # Compaction hint (gated by profile component).
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
    else:
        remaining = max_rounds - round_num
        if remaining > 0:
            print(f"  Next: Address findings in FINDINGS_Sprint{sprint}.md, then re-run:")
            print(f'        ./scripts/council-review.py {review_type} {sprint} "{title}"')
            print(f"        ({remaining} round(s) remaining before forced approval)")
        else:
            print(f"  ESCALATION: Max rounds reached. Present unresolved findings to the human.")
            print(f"  Options: cut scope, override with higher max_rounds, or accept Known Debt.")


if __name__ == "__main__":
    main()

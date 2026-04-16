#!/usr/bin/env python3
"""File: scripts/bootstrap-smoke.py

Purpose: End-to-end smoke test for scripts/bootstrap.py. Builds a
virtual template from the current dev-container (or uses the checkout
itself when --skip-virtual is set), runs bootstrap with canned
answers, and asserts the resulting project passes its own
check-headers + a small slice of pytest.

Role:
  CI-facing entry point for bootstrap regression coverage. Designed
  to run without API keys: bootstrap's Claude-CLI calls fail-soft to
  placeholders, and the canned answers keep the wizard on the
  no-knowledge, no-council path so no external services are touched.

Exports:
  - build_virtual_template -- copy manifest-listed paths + seeds into a tmpdir
  - run_smoke -- orchestrator (returns 0 on success)
  - main -- CLI entry point

Depends on:
  - internal: scripts/publish-template.py (for manifest + file-list)
  - external: git (only for init of the virtual template)

Invariants & gotchas:
  - --profile {minimal,standard,full} must match the profile that
    the answers file is valid for (currently all three share a single
    no-council, python-only answers file; see
    tests/fixtures/bootstrap_answers/minimal.json).
  - Subprocess invocations use timeout= to prevent CI hangs. If any
    invocation times out, the script exits non-zero with the captured
    stderr so the failure is diagnosable from CI logs.

Last updated: Sprint 5 (2026-04-16) -- initial smoke runner.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "scripts" / "template-manifest.json"

# Inlined default answers (minimal, no-council, python-only bootstrap).
# Previously loaded from tests/fixtures/bootstrap_answers/minimal.json,
# but the fixture is template-excluded (test infrastructure that
# downstream users don't need), which broke the template repo's CI.
# Baking defaults into the script keeps it self-contained.
DEFAULT_ANSWERS: dict = {
    "identity.project_name": "bootstrap_smoke_project",
    "identity.mvp_outcome": (
        "ship bootstrap-smoke so the template surface stays self-sufficient"
    ),
    "identity.has_brief": False,
    "stack.languages": ["python"],
    "stack.framework.python": "none",
    "knowledge.has_files": False,
    "sprints.mode": "Skip — I'll do this later",
    "council.review_mode": (
        "Skip council entirely — ship as solo dev without reviewer"
    ),
    "bootstrap.rerun": False,
}

EXIT_OK = 0
EXIT_BOOTSTRAP = 1
EXIT_CHECK_HEADERS = 2
EXIT_PYTEST = 3


def _load_publish_template_module():
    """Try to import publish-template.py. Returns None when it isn't on
    disk — this happens when the script runs from a bootstrapped /
    template repo where publish-template.py is dev-tooling that
    doesn't ship."""
    pt_path = REPO_ROOT / "scripts" / "publish-template.py"
    if not pt_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_pt", pt_path)
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so @dataclass can resolve
    # __module__ on the SyncResult class it defines.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_GITIGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".claude",
    "council", "Documentation",  # Documentation excluded: archive is huge
}


def _copy_current_repo(dest: Path) -> None:
    """Copy the current repo (REPO_ROOT) into ``dest``, filtering out
    git state, caches, and dev-container-only directories that a
    freshly-bootstrapped project would never see."""
    for item in REPO_ROOT.iterdir():
        if item.name in _GITIGNORE_DIRS or item.name.startswith("."):
            # Preserve .github, .mcp.json, .gitignore, .claude/skills
            # but skip other dotfiles/dirs.
            if item.name not in {".github", ".mcp.json", ".gitignore"}:
                continue
        dst = dest / item.name
        if item.is_dir():
            shutil.copytree(
                item, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache",
                    ".ruff_cache", "worktrees",
                ),
            )
        else:
            shutil.copy2(item, dst)


def build_virtual_template(dest: Path) -> None:
    """Copy the manifest-listed template surface into ``dest``, then
    git-init so the resulting repo matches what a downstream user sees
    after ``gh repo create --template``.

    When `publish-template.py` is present (dev-container), we use the
    manifest to build the exact template surface. When it isn't
    (running from the template itself, or a downstream project), we
    copy the current repo minus caches — that IS the template surface
    in those contexts.
    """
    pt = _load_publish_template_module()
    if pt is not None:
        manifest = pt.load_manifest(MANIFEST_PATH)
        pt.validate_manifest(manifest, REPO_ROOT)
        for rel in pt.compute_file_list(manifest, REPO_ROOT):
            src = REPO_ROOT / rel
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for dest_rel, seed_rel in manifest["seeded_files"].items():
            src = REPO_ROOT / seed_rel
            dst = dest / dest_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    else:
        _copy_current_repo(dest)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }
    subprocess.run(
        ["git", "-C", str(dest), "init", "-b", "main"],
        check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(dest), "add", "-A"],
        check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "virtual template"],
        check=True, capture_output=True, env=env,
    )


def _die(stream: str, header: str, proc: subprocess.CompletedProcess) -> None:
    print(f"\n=== {header} ===", file=sys.stderr)
    print(f"exit code: {proc.returncode}", file=sys.stderr)
    print("--- stdout ---", file=sys.stderr)
    print(proc.stdout, file=sys.stderr)
    print("--- stderr ---", file=sys.stderr)
    print(proc.stderr, file=sys.stderr)


def run_smoke(
    profile: str,
    answers_path: Path | None = None,
    tmpdir: Path | None = None,
) -> int:
    """Orchestrate one smoke run. Returns a POSIX exit code.

    When ``answers_path`` is None, the inlined DEFAULT_ANSWERS are
    written to the virtual repo as answers.json. When a path is given
    and doesn't exist, we fail loudly rather than silently falling
    back (the caller asked for specific answers).
    """
    if answers_path is not None and not answers_path.is_file():
        print(
            f"answers file not found: {answers_path}", file=sys.stderr
        )
        return EXIT_BOOTSTRAP

    owns_tmp = tmpdir is None
    tmp_ctx: tempfile.TemporaryDirectory | None = None
    if owns_tmp:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="bootstrap-smoke-")
        workdir = Path(tmp_ctx.name)
    else:
        workdir = tmpdir
        workdir.mkdir(parents=True, exist_ok=True)

    try:
        virtual = workdir / "virtual"
        virtual.mkdir()
        build_virtual_template(virtual)

        # Answers: use supplied path if given, else write the inlined
        # DEFAULT_ANSWERS. bootstrap requires the answers file to
        # resolve inside cwd, so we always write/copy into `virtual/`.
        local_answers = virtual / "answers.json"
        if answers_path is not None:
            shutil.copy2(answers_path, local_answers)
        else:
            local_answers.write_text(
                json.dumps(DEFAULT_ANSWERS, indent=2) + "\n"
            )

        bootstrap_result = subprocess.run(
            [
                sys.executable,
                str(virtual / "scripts" / "bootstrap.py"),
                "--profile", profile,
                "--answers-file", "answers.json",
            ],
            cwd=virtual, capture_output=True, text=True, timeout=180,
        )
        if bootstrap_result.returncode != 0:
            _die("", "bootstrap failed", bootstrap_result)
            return EXIT_BOOTSTRAP
        if not (virtual / ".bootstrap-complete").is_file():
            print(
                "bootstrap succeeded but .bootstrap-complete is missing",
                file=sys.stderr,
            )
            return EXIT_BOOTSTRAP

        ch_result = subprocess.run(
            [sys.executable, "scripts/check-headers.py"],
            cwd=virtual, capture_output=True, text=True, timeout=60,
        )
        if ch_result.returncode != 0:
            _die("", "check-headers failed", ch_result)
            return EXIT_CHECK_HEADERS

        # Small pytest slice that works under every profile (tests that
        # don't import the council / skills / digest modules, which the
        # minimal profile removes). See test_template_bootstrap.py for
        # the same rationale.
        pytest_slice = [
            "tests/test_profile_cli.py",
            "tests/test_settings.py",
        ]
        # Only include files that actually ship under the chosen profile.
        available = [p for p in pytest_slice if (virtual / p).is_file()]
        if available:
            pytest_result = subprocess.run(
                [sys.executable, "-m", "pytest", *available, "-q"],
                cwd=virtual, capture_output=True, text=True, timeout=120,
            )
            if pytest_result.returncode != 0:
                _die("", "pytest failed", pytest_result)
                return EXIT_PYTEST

        summary = (
            f"bootstrap-smoke OK -- profile={profile} "
            f"(bootstrap, check-headers, pytest {len(available)} file(s))"
        )
        print(summary)
        return EXIT_OK
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run scripts/bootstrap.py end-to-end against a virtual "
            "template built from scripts/template-manifest.json."
        )
    )
    parser.add_argument(
        "--profile", choices=["minimal", "standard", "full"],
        required=True,
    )
    parser.add_argument(
        "--answers",
        default=None,
        help="Path to a canned answers JSON file (default: inlined "
             "minimal answers baked into this script).",
    )
    parser.add_argument(
        "--tmpdir",
        default=None,
        help="Existing directory to build the virtual template under "
             "(default: a TemporaryDirectory that is cleaned up on exit).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    tmp_arg = Path(args.tmpdir) if args.tmpdir else None
    answers_arg = Path(args.answers) if args.answers else None
    return run_smoke(
        profile=args.profile,
        answers_path=answers_arg,
        tmpdir=tmp_arg,
    )


if __name__ == "__main__":
    sys.exit(main())

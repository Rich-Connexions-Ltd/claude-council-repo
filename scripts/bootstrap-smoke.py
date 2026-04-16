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
DEFAULT_ANSWERS = (
    REPO_ROOT / "tests" / "fixtures" / "bootstrap_answers" / "minimal.json"
)

EXIT_OK = 0
EXIT_BOOTSTRAP = 1
EXIT_CHECK_HEADERS = 2
EXIT_PYTEST = 3


def _load_publish_template_module():
    spec = importlib.util.spec_from_file_location(
        "_pt", REPO_ROOT / "scripts" / "publish-template.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so @dataclass can resolve
    # __module__ on the SyncResult class it defines.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def build_virtual_template(dest: Path) -> None:
    """Copy the manifest-listed template surface into ``dest``, then
    git-init so the resulting repo matches what a downstream user sees
    after ``gh repo create --template``."""
    pt = _load_publish_template_module()
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
    profile: str, answers_path: Path, tmpdir: Path | None = None
) -> int:
    """Orchestrate one smoke run. Returns a POSIX exit code."""
    if not answers_path.is_file():
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

        # Copy the answers file into the virtual repo — bootstrap requires
        # the file to resolve inside cwd.
        local_answers = virtual / "answers.json"
        shutil.copy2(answers_path, local_answers)

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
        default=str(DEFAULT_ANSWERS),
        help="Path to a canned answers JSON file (default: "
             "tests/fixtures/bootstrap_answers/minimal.json).",
    )
    parser.add_argument(
        "--tmpdir",
        default=None,
        help="Existing directory to build the virtual template under "
             "(default: a TemporaryDirectory that is cleaned up on exit).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    tmp_arg = Path(args.tmpdir) if args.tmpdir else None
    return run_smoke(
        profile=args.profile,
        answers_path=Path(args.answers),
        tmpdir=tmp_arg,
    )


if __name__ == "__main__":
    sys.exit(main())

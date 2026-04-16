"""File: tests/test_guardrails.py

Purpose: Property matrix + failure-path coverage for the guardrails PreToolUse hook.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "hooks" / "guardrails.py"


def _run(payload: dict, mode: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if mode is not None:
        env["COUNCIL_HOOK_PROFILE"] = mode
    else:
        env.pop("COUNCIL_HOOK_PROFILE", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )


def _bash(cmd: str, cwd: str | None = None) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": cwd or str(REPO_ROOT),
    }


@pytest.mark.parametrize("mode,expected_exit,expect_stderr", [
    ("off", 0, False),
    ("warn", 0, True),
    ("strict", 2, True),
])
def test_no_verify_matrix(mode, expected_exit, expect_stderr):
    r = _run(_bash("git commit -m x --no-verify"), mode=mode)
    assert r.returncode == expected_exit
    assert bool(r.stderr.strip()) == expect_stderr


@pytest.mark.parametrize("mode,expected_exit", [
    ("off", 0), ("warn", 0), ("strict", 2),
])
def test_force_push_matrix(mode, expected_exit):
    r = _run(_bash("git push origin main --force"), mode=mode)
    assert r.returncode == expected_exit


def test_force_with_lease_matched():
    r = _run(_bash("git push --force-with-lease origin main"), mode="strict")
    assert r.returncode == 2


def test_rm_rf_tmp_allowed():
    r = _run(_bash("rm -rf /tmp/foo"), mode="strict")
    assert r.returncode == 0


def test_rm_rf_node_modules_allowed(tmp_path):
    (tmp_path / "node_modules").mkdir()
    r = _run(_bash("rm -rf node_modules", cwd=str(tmp_path)), mode="strict")
    assert r.returncode == 0


def test_rm_rf_canonical_path_outside_blocked():
    r = _run(_bash("rm -rf /Users/nonexistent-something-outside"), mode="strict")
    assert r.returncode == 2


def test_rm_rf_relative_traversal_blocked():
    """The hook validates commands — it does not run rm — so the cwd
    does not need to exist on disk. We deliberately use a repo-anchored
    path: on Linux CI, ``tmp_path`` is under ``/tmp`` which is in the
    rule's allow-list, so a subdir of tmp_path would still resolve to
    an allowed location. Using REPO_ROOT/nonexistent/sub makes the
    test hold on macOS (TMPDIR under /var/folders) and Linux (/tmp is
    not the repo parent)."""
    sub = REPO_ROOT / "nonexistent-sub-for-rm-test" / "sub"
    r = _run(_bash("rm -rf ../..", cwd=str(sub)), mode="strict")
    assert r.returncode == 2


def test_non_bash_tool_ignored():
    r = _run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}, mode="strict")
    assert r.returncode == 0


def test_malformed_stdin_fails_open():
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True, text=True,
    )
    assert r.returncode == 0


def test_empty_command_ignored():
    r = _run(_bash(""), mode="strict")
    assert r.returncode == 0


def test_unknown_mode_defaults_to_warn():
    r = _run(_bash("git commit --no-verify"), mode="screaming")
    assert r.returncode == 0
    assert "unknown COUNCIL_HOOK_PROFILE" in r.stderr


def test_off_mode_suppresses_all():
    r = _run(_bash("git commit --no-verify"), mode="off")
    assert r.returncode == 0
    assert r.stderr == ""


def test_eval_obfuscation_not_matched():
    """Documented limitation: guardrails are advisory; a user can bypass
    via eval / bash -c / aliases. Asserting this keeps the limit explicit."""
    r = _run(_bash("eval 'git commit --no-verify'"), mode="strict")
    assert r.returncode == 0

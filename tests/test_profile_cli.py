"""File: tests/test_profile_cli.py

Purpose: Assert scripts/profile.py CLI contract (exit codes for is-enabled).

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PY = REPO_ROOT / "scripts" / "profile.py"


def _run(dest: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(dest / "scripts" / "profile.py"), *args],
        capture_output=True, text=True, cwd=dest,
    )


def test_is_enabled_exit_zero_for_enabled(bootstrap_standard):
    r = _run(bootstrap_standard, "is-enabled", "council")
    assert r.returncode == 0


def test_is_enabled_exit_one_for_disabled(bootstrap_minimal):
    r = _run(bootstrap_minimal, "is-enabled", "council")
    assert r.returncode == 1


def test_is_enabled_exit_two_for_unknown(bootstrap_standard):
    r = _run(bootstrap_standard, "is-enabled", "nonexistent-component")
    assert r.returncode == 2


def test_is_enabled_missing_arg(bootstrap_standard):
    r = _run(bootstrap_standard, "is-enabled")
    assert r.returncode == 2


def test_unknown_subcommand(bootstrap_standard):
    r = _run(bootstrap_standard, "bogus")
    assert r.returncode == 2


def test_absent_profile_file_defaults(tmp_path, monkeypatch):
    # Copy just profiles.json into an otherwise empty dir; no .claude/project-profile.
    (tmp_path / "scripts" / "bootstrap").mkdir(parents=True)
    (tmp_path / "scripts" / "bootstrap" / "profiles.json").write_text(
        (REPO_ROOT / "scripts" / "bootstrap" / "profiles.json").read_text()
    )
    import shutil
    shutil.copy2(PROFILE_PY, tmp_path / "scripts" / "profile.py")
    r = subprocess.run(
        [sys.executable, "scripts/profile.py", "current"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "standard"
    assert "absent" in r.stderr

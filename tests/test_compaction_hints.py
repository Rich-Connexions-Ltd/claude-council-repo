"""File: tests/test_compaction_hints.py

Purpose: Compaction hint emitted on APPROVED paths when profile has
compaction enabled; silent otherwise.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _prep_archive(dest: Path, sprint: str = "77") -> None:
    shutil.copy2(REPO_ROOT / "scripts" / "archive-plan.sh",
                 dest / "scripts" / "archive-plan.sh")
    (dest / f"PLAN_Sprint{sprint}.md").write_text("# plan\n")
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=dest, check=True,
    )


def test_archive_hint_on_standard(bootstrap_standard):
    dest = bootstrap_standard
    _prep_archive(dest)
    r = subprocess.run(
        ["bash", "scripts/archive-plan.sh", "77", "Test"],
        cwd=dest, capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "/compact" in r.stderr


def test_archive_no_hint_on_minimal(bootstrap_minimal):
    dest = bootstrap_minimal
    _prep_archive(dest)
    r = subprocess.run(
        ["bash", "scripts/archive-plan.sh", "77", "Test"],
        cwd=dest, capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "/compact" not in r.stderr


def test_archive_no_hint_on_failure(bootstrap_standard):
    dest = bootstrap_standard
    # Initialise git and set up state, but deliberately omit the PLAN file
    # so archive-plan.sh exits on its "PLAN_Sprint<N>.md not found" guard.
    shutil.copy2(REPO_ROOT / "scripts" / "archive-plan.sh",
                 dest / "scripts" / "archive-plan.sh")
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=dest, check=True,
    )
    assert not (dest / "PLAN_Sprint66.md").exists()
    r = subprocess.run(
        ["bash", "scripts/archive-plan.sh", "66", "Test"],
        cwd=dest, capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "not found" in (r.stderr + r.stdout) or "Nothing to archive" in (r.stderr + r.stdout)
    assert "/compact" not in r.stderr

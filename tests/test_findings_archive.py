"""File: tests/test_findings_archive.py

Purpose: Regression guard that archive-plan.sh preserves FINDINGS_Sprint<N>.md
under Documentation/findings-archive/ when the findings_archive component is
enabled (standard/full), and skips the copy on minimal.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = REPO_ROOT / "scripts" / "archive-plan.sh"


def _prep(dest: Path, sprint: str = "99") -> None:
    """Stage a fake approved plan + findings file ready for archive."""
    shutil.copy2(ARCHIVE, dest / "scripts" / "archive-plan.sh")
    (dest / f"PLAN_Sprint{sprint}.md").write_text("# plan\n")
    (dest / f"FINDINGS_Sprint{sprint}.md").write_text(
        "# Findings Tracker: Sprint %s (plan)\n\n"
        "| # | Round | Severity | Lens | Tag | Finding | Status | Resolution |\n"
        "|---|-------|----------|------|-----|---------|--------|------------|\n"
        "| 1 | R1 | High | security | test | sample | ADDRESSED |  |\n" % sprint
    )
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=dest, check=True,
    )


def test_archive_copies_findings_file(bootstrap_standard):
    dest = bootstrap_standard
    _prep(dest)
    r = subprocess.run(
        ["bash", "scripts/archive-plan.sh", "99", "Test"],
        cwd=dest, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    archived = dest / "Documentation" / "findings-archive" / "FINDINGS_Sprint99.md"
    assert archived.exists(), "archive-plan.sh did not preserve FINDINGS file"
    # Original is still removed from working tree
    assert not (dest / "FINDINGS_Sprint99.md").exists()


def test_minimal_skips_findings_archive(bootstrap_minimal):
    dest = bootstrap_minimal
    _prep(dest)
    r = subprocess.run(
        ["bash", "scripts/archive-plan.sh", "99", "Test"],
        cwd=dest, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    archived = dest / "Documentation" / "findings-archive" / "FINDINGS_Sprint99.md"
    assert not archived.exists(), "minimal profile should not preserve FINDINGS"

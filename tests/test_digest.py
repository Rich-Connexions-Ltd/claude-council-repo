"""File: tests/test_digest.py

Purpose: Cover findings-digest invariants — deterministic body, grouping by
(lens, tag), resolved-but-recurring classification, malformed-file tolerance,
empty-input handling.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "findings"


def _stage(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "Documentation" / "findings-archive").mkdir(parents=True)
    for f in FIXTURE_DIR.glob("*.md"):
        shutil.copy2(f, proj / "Documentation" / "findings-archive" / f.name)
    # Copy the digest script + relative pathing
    (proj / "scripts").mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "findings-digest.py",
                 proj / "scripts" / "findings-digest.py")
    return proj


def _run(proj: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(proj / "scripts" / "findings-digest.py")],
        capture_output=True, text=True, cwd=proj,
    )


def _strip_timestamp(body: str) -> str:
    return re.sub(r"_Generated [^_]+_", "_TS_", body)


def test_empty_input_valid_digest(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    (proj / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "findings-digest.py",
                 proj / "scripts" / "findings-digest.py")
    r = _run(proj)
    assert r.returncode == 0
    out = (proj / "Documentation" / "FINDINGS_DIGEST.md").read_text()
    assert "Findings Digest" in out
    assert "None yet." in out


def test_deterministic_body(tmp_path):
    proj = _stage(tmp_path)
    r1 = _run(proj)
    assert r1.returncode == 0
    body1 = (proj / "Documentation" / "FINDINGS_DIGEST.md").read_text()
    r2 = _run(proj)
    body2 = (proj / "Documentation" / "FINDINGS_DIGEST.md").read_text()
    assert _strip_timestamp(body1) == _strip_timestamp(body2)


def test_malformed_file_skipped(tmp_path):
    proj = _stage(tmp_path)
    r = _run(proj)
    assert r.returncode == 0
    assert "no findings table" in r.stderr or "skipping" in r.stderr


def test_resolved_but_recurring_detected(tmp_path):
    proj = _stage(tmp_path)
    _run(proj)
    body = (proj / "Documentation" / "FINDINGS_DIGEST.md").read_text()
    assert "Resolved-but-recurring" in body
    # auth-token-leak: ADDRESSED in 10, OPEN in 11 → must appear
    recurring_section = body.split("Resolved-but-recurring")[1]
    assert "auth-token-leak" in recurring_section


def test_watchlist_vs_actionable(tmp_path):
    proj = _stage(tmp_path)
    _run(proj)
    body = (proj / "Documentation" / "FINDINGS_DIGEST.md").read_text()
    actionable = body.split("watchlist")[0]
    watchlist = body.split("watchlist")[1].split("Resolved-but-recurring")[0]
    # naming-drift: 3 sprints (10, 11, 12) → actionable
    assert "naming-drift" in actionable
    # solo-theme: 1 sprint → neither actionable nor watchlist
    assert "solo-theme" not in actionable
    assert "solo-theme" not in watchlist

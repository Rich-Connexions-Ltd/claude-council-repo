"""File: tests/test_convergence_reporting.py

Purpose: Regression guard for Sprint 1 finding #24 — compute_convergence_score
must surface RECURRING counts in its description when any such findings
exist, so the single-line convergence summary conveys oscillation state.

Last updated: Sprint 5 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest


TRACKER_HEADER = """# Findings Tracker: Sprint 99 (plan)

Editor: update as you go.

| # | Round | Severity | Lens | Tag | Finding | Status | Resolution |
|---|-------|----------|------|-----|---------|--------|------------|
"""


def _row(fid: int, severity: str, status: str, description: str = "issue") -> str:
    return f"| {fid} | R1 | {severity} | code_quality | tag | {description} | {status} |  |\n"


def _write_tracker(path: Path, rows: list[str]) -> None:
    path.write_text(TRACKER_HEADER + "".join(rows))


@pytest.fixture
def cr(council_review_module):
    return council_review_module


def test_no_tracker_returns_zero(cr, tmp_path):
    score, desc = cr.compute_convergence_score(tmp_path / "absent.md")
    assert score == 0.0
    assert desc == "No tracker"


def test_empty_tracker_returns_one(cr, tmp_path):
    tracker = tmp_path / "tracker.md"
    _write_tracker(tracker, [])
    score, desc = cr.compute_convergence_score(tracker)
    assert score == 1.0
    assert desc == "No findings"


def test_description_has_core_columns(cr, tmp_path):
    tracker = tmp_path / "tracker.md"
    _write_tracker(tracker, [
        _row(1, "High", "ADDRESSED"),
        _row(2, "Medium", "OPEN"),
        _row(3, "Low", "REOPENED"),
    ])
    score, desc = cr.compute_convergence_score(tracker)
    assert "resolved" in desc
    assert "open" in desc
    assert "reopened" in desc
    # No recurring in this tracker → clause absent
    assert "recurring" not in desc
    assert "1/3 resolved" in desc
    assert "1 open" in desc
    assert "1 reopened" in desc
    assert score == pytest.approx(1 / 3)


def test_recurring_finding_surfaces_in_description(cr, tmp_path):
    tracker = tmp_path / "tracker.md"
    _write_tracker(tracker, [
        _row(1, "High", "ADDRESSED"),
        _row(2, "Medium", "RECURRING"),
    ])
    score, desc = cr.compute_convergence_score(tracker)
    assert "1 recurring" in desc, (
        f"RECURRING count missing from convergence desc: {desc!r}"
    )
    # RECURRING does not count as resolved.
    assert "1/2 resolved" in desc


def test_multiple_recurring_counted(cr, tmp_path):
    tracker = tmp_path / "tracker.md"
    _write_tracker(tracker, [
        _row(1, "High", "RECURRING"),
        _row(2, "Medium", "RECURRING"),
        _row(3, "Low", "ADDRESSED"),
    ])
    _, desc = cr.compute_convergence_score(tracker)
    assert "2 recurring" in desc


def test_zero_recurring_clause_is_omitted(cr, tmp_path):
    """Healthy sprint (no RECURRING) → clause absent to keep output terse."""
    tracker = tmp_path / "tracker.md"
    _write_tracker(tracker, [
        _row(1, "High", "VERIFIED"),
        _row(2, "Medium", "ADDRESSED"),
    ])
    _, desc = cr.compute_convergence_score(tracker)
    assert "recurring" not in desc, (
        "Zero-recurring sprints should not mention recurring at all"
    )

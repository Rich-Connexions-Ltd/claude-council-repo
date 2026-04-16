"""File: tests/test_findings_schema.py

Purpose: Round-trip the findings tracker schema v2 (with Lens/Tag) and
backward-compat read of schema v1.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

from pathlib import Path


def test_roundtrip_v2(tmp_path, council_review_module):
    path = tmp_path / "FINDINGS_Sprint99.md"
    findings = [
        {"id": 1, "round": 1, "severity": "High", "lens": "security",
         "tag": "no-verify-bypass", "description": "Example",
         "status": "OPEN", "resolution": ""},
    ]
    council_review_module._write_tracker(path, "99", findings, "plan")
    parsed = council_review_module._read_tracker(path)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["lens"] == "security"
    assert p["tag"] == "no-verify-bypass"
    assert p["severity"] == "High"
    assert p["status"] == "OPEN"


def test_read_v1_tracker_gets_defaults(tmp_path, council_review_module):
    path = tmp_path / "FINDINGS_SprintOld.md"
    # Old 6-column format
    path.write_text(
        "# Findings Tracker: Sprint Old (plan)\n\n"
        "| # | Round | Severity | Finding | Status | Resolution |\n"
        "|---|-------|----------|---------|--------|------------|\n"
        "| 1 | R1 | High | Old finding | OPEN |  |\n"
    )
    parsed = council_review_module._read_tracker(path)
    assert len(parsed) == 1
    assert parsed[0]["lens"] == "unknown"
    assert parsed[0]["tag"] == "untagged"
    assert parsed[0]["description"] == "Old finding"


def test_parse_findings_populates_lens_tag(council_review_module):
    review = (
        "### Findings\n"
        "- **[High]** Example finding about profiles (Source: code_quality, domain)\n"
        "- **[Medium]** Another thing (Source: security)\n"
    )
    findings = council_review_module._parse_findings(review, 1)
    assert len(findings) == 2
    assert findings[0]["lens"] == "code_quality"
    assert findings[0]["tag"].startswith("example-finding")
    assert findings[1]["lens"] == "security"

"""File: tests/test_profiles.py

Purpose: Assert profile membership, bootstrap file gating, and persisted profile format.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_COMPONENTS = {
    "minimal": {"headers", "codegraph"},
    "standard": {"headers", "codegraph", "council", "skills", "guardrails",
                 "compaction", "findings_archive"},
    "full": {"headers", "codegraph", "council", "skills", "guardrails",
             "compaction", "findings_archive", "digest", "metrics_digest"},
}


def test_metrics_digest_only_in_full_profile():
    """Sprint 6 R2 #25: metrics_digest (scripts/council-metrics-digest.py)
    is full-only — the feature is advisory and not worth carrying in
    minimal/standard. Regression guard for the profile gating."""
    data = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "profiles.json").read_text())
    assert "metrics_digest" in data["profiles"]["full"]["components"]
    assert "metrics_digest" not in data["profiles"]["standard"]["components"]
    assert "metrics_digest" not in data["profiles"]["minimal"]["components"]


def test_profiles_json_matches_plan_matrix():
    data = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "profiles.json").read_text())
    for name, expected in EXPECTED_COMPONENTS.items():
        actual = set(data["profiles"][name]["components"])
        assert actual == expected, f"profile {name}: expected {expected}, got {actual}"


def test_profile_monotonicity():
    data = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "profiles.json").read_text())
    minimal = set(data["profiles"]["minimal"]["components"])
    standard = set(data["profiles"]["standard"]["components"])
    full = set(data["profiles"]["full"]["components"])
    assert minimal <= standard <= full


def test_component_files_covers_all_components():
    profiles = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "profiles.json").read_text())
    component_files = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "component_files.json").read_text())
    all_from_profiles: set[str] = set()
    for p in profiles["profiles"].values():
        all_from_profiles.update(p["components"])
    known = set(component_files["components"].keys())
    assert all_from_profiles == known, f"profiles: {all_from_profiles}, component_files: {known}"


def test_component_files_paths_are_repo_relative():
    cf = json.loads((REPO_ROOT / "scripts" / "bootstrap" / "component_files.json").read_text())
    for comp, entry in cf["components"].items():
        for rel in entry.get("files", []):
            assert not rel.startswith("/"), f"{comp}: absolute path {rel!r}"
            assert ".." not in Path(rel).parts, f"{comp}: parent traversal in {rel!r}"


def test_minimal_has_no_council_leftovers(bootstrap_minimal):
    dest = bootstrap_minimal
    assert not (dest / "scripts" / "council-review.py").exists()
    assert not (dest / "scripts" / "council-config.json").exists()
    assert not (dest / "scripts" / "council-check.sh").exists()
    assert not (dest / "scripts" / "hooks" / "guardrails.py").exists()
    assert not (dest / "scripts" / "findings-digest.py").exists()
    # headers + codegraph retained
    assert (dest / "scripts" / "bump-header.py").exists()


def test_standard_includes_council_not_digest(bootstrap_standard):
    dest = bootstrap_standard
    assert (dest / "scripts" / "council-review.py").exists()
    assert (dest / "scripts" / "hooks" / "guardrails.py").exists()
    assert not (dest / "scripts" / "findings-digest.py").exists()


def test_full_includes_digest(bootstrap_full):
    dest = bootstrap_full
    assert (dest / "scripts" / "findings-digest.py").exists()
    assert (dest / "scripts" / "council-review.py").exists()


def test_project_profile_persisted(bootstrap_standard):
    pp = bootstrap_standard / ".claude" / "project-profile"
    assert pp.exists()
    data = json.loads(pp.read_text())
    assert data == {"profile": "standard", "schema_version": 1}


def test_invalid_profile_rejected(bootstrap_module):
    with pytest.raises(SystemExit):
        bootstrap_module._resolve_profile("other")


def test_safe_remove_bounds_check(tmp_path, bootstrap_module, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap_module, "REPO_ROOT", tmp_path)
    outside = tmp_path.parent / "external.txt"
    outside.write_text("untouched")
    bootstrap_module._safe_remove(outside)
    assert outside.exists()
    captured = capsys.readouterr()
    assert "refusing" in captured.err

"""File: tests/test_settings.py

Purpose: Assert generated .claude/settings.json is valid JSON and has the
right hooks wired per profile.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

import json
from pathlib import Path


def _read_settings(dest):
    return json.loads((dest / ".claude" / "settings.json").read_text())


def test_minimal_settings_have_posttooluse_only(bootstrap_minimal):
    s = _read_settings(bootstrap_minimal)
    hooks = s.get("hooks", {})
    assert "PreToolUse" not in hooks
    post = hooks.get("PostToolUse", [])
    assert any("bump-header" in h.get("hooks", [{}])[0].get("command", "") for h in post)


def test_standard_has_both_hooks(bootstrap_standard):
    s = _read_settings(bootstrap_standard)
    hooks = s["hooks"]
    assert any("guardrails" in entry["hooks"][0]["command"] for entry in hooks["PreToolUse"])
    assert any("bump-header" in entry["hooks"][0]["command"] for entry in hooks["PostToolUse"])


def test_full_same_as_standard_hook_wise(bootstrap_full):
    s = _read_settings(bootstrap_full)
    hooks = s["hooks"]
    assert "PreToolUse" in hooks and "PostToolUse" in hooks


def test_settings_json_is_valid_json(bootstrap_standard):
    raw = (bootstrap_standard / ".claude" / "settings.json").read_text()
    json.loads(raw)  # raises on error


def test_no_component_leakage_in_output(bootstrap_standard):
    raw = (bootstrap_standard / ".claude" / "settings.json").read_text()
    assert "_component" not in raw
    assert "_comment" not in raw

"""File: tests/test_index_codebase.py

Purpose: Coverage for scripts/index-codebase.py helpers extracted
during Sprint 6 — render_context_table (truncation boundary),
_parse_max_lines_arg (validation).

Last updated: Sprint 6 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IC_PATH = REPO_ROOT / "scripts" / "index-codebase.py"


@pytest.fixture(scope="module")
def ic_module():
    spec = importlib.util.spec_from_file_location("ic_mod", IC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ic_mod"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# render_context_table — truncation boundary (R2 #13)
# ---------------------------------------------------------------------------


def test_no_marker_when_under_budget(ic_module):
    body = "\n".join(f"line-{i}" for i in range(5))
    out = ic_module.render_context_table(body, max_lines=10)
    assert out == body
    assert "omitted" not in out


def test_no_marker_at_exact_boundary(ic_module):
    """10 lines, max=10 → no marker."""
    body = "\n".join(f"line-{i}" for i in range(10))
    out = ic_module.render_context_table(body, max_lines=10)
    assert out == body
    assert "omitted" not in out


def test_marker_at_one_over_boundary(ic_module):
    """11 lines, max=10 → keep first 10 + marker mentioning 1 line."""
    body = "\n".join(f"line-{i}" for i in range(11))
    out = ic_module.render_context_table(body, max_lines=10)
    kept = out.splitlines()
    assert kept[0] == "line-0"
    assert kept[9] == "line-9"
    assert kept[10].startswith("... (1 line omitted;")


def test_marker_plural_for_many_omitted(ic_module):
    body = "\n".join(f"line-{i}" for i in range(100))
    out = ic_module.render_context_table(body, max_lines=10)
    kept = out.splitlines()
    assert kept[-1].startswith("... (90 lines omitted;")


def test_render_does_not_corrupt_utf8(ic_module):
    body = "ümlaut\n✓ok\ntail"
    out = ic_module.render_context_table(body, max_lines=5)
    assert out == body


# ---------------------------------------------------------------------------
# _parse_max_lines_arg
# ---------------------------------------------------------------------------


def test_default_when_flag_absent(ic_module):
    n = ic_module._parse_max_lines_arg(["--context-for", "a.py"])
    assert n == ic_module.DEFAULT_CONTEXT_MAX_LINES


def test_positive_integer_accepted(ic_module):
    n = ic_module._parse_max_lines_arg(
        ["--context-for", "a.py", "--max-lines", "50"]
    )
    assert n == 50


def test_zero_rejected(ic_module):
    with pytest.raises(SystemExit):
        ic_module._parse_max_lines_arg(
            ["--context-for", "a.py", "--max-lines", "0"]
        )


def test_negative_rejected(ic_module):
    with pytest.raises(SystemExit):
        ic_module._parse_max_lines_arg(
            ["--context-for", "a.py", "--max-lines", "-10"]
        )


def test_non_integer_rejected(ic_module):
    with pytest.raises(SystemExit):
        ic_module._parse_max_lines_arg(
            ["--context-for", "a.py", "--max-lines", "many"]
        )


def test_missing_value_rejected(ic_module):
    with pytest.raises(SystemExit):
        ic_module._parse_max_lines_arg(
            ["--context-for", "a.py", "--max-lines"]
        )

"""File: tests/test_call_codex.py

Purpose: Regression guard for Sprint 1 finding #23 — call_codex must
not leave orphaned tempfiles behind and must not reference tempfile at
all (the prompt is piped via subprocess stdin).

Last updated: Sprint 5 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNCIL_REVIEW_PATH = REPO_ROOT / "scripts" / "council-review.py"


@pytest.fixture
def cr(council_review_module):
    return council_review_module


def test_call_codex_no_tempfile_leak(cr, monkeypatch, tmp_path):
    """After a successful mock invocation, no files owned by call_codex
    remain under the tempdir."""

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = str(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "ok-output"
        stderr = ""

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(cr.subprocess, "run", fake_run)

    before = set(tmp_path.iterdir())
    out = cr.call_codex("sys-prompt", "user-content", timeout=5.0)
    after = set(tmp_path.iterdir())

    assert out == "ok-output"
    assert before == after, (
        f"call_codex left files in tempdir. "
        f"new entries: {after - before}"
    )
    # Sanity: the combined prompt was passed via stdin, not a file path.
    assert captured["kwargs"]["input"].startswith("sys-prompt")
    assert captured["kwargs"]["input"].endswith("user-content")


def test_call_codex_source_has_no_tempfile_reference():
    """Static guard: call_codex's executable body must not mention
    NamedTemporaryFile or prompt_file (dead-code removed in Sprint 5).

    Skips the docstring node so that the explanation of *why* the
    tempfile was removed doesn't trip the check.
    """
    src = COUNCIL_REVIEW_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "call_codex":
            body_nodes = node.body
            if (body_nodes and isinstance(body_nodes[0], ast.Expr)
                    and isinstance(body_nodes[0].value, ast.Constant)
                    and isinstance(body_nodes[0].value.value, str)):
                body_nodes = body_nodes[1:]
            body_src = "\n".join(
                ast.get_source_segment(src, n) or "" for n in body_nodes
            )
            assert "NamedTemporaryFile" not in body_src, (
                "call_codex should no longer create a tempfile"
            )
            assert "prompt_file" not in body_src, (
                "call_codex should no longer reference prompt_file"
            )
            return
    pytest.fail("call_codex not found in council-review.py")


def test_call_codex_timeout_path_leaks_no_tempfile(cr, monkeypatch, tmp_path):
    """Even when subprocess.run raises TimeoutExpired, no tempfile
    should remain."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.tempdir = str(tmp_path)

    def fake_run(*args, **kwargs):
        raise cr.subprocess.TimeoutExpired(cmd=["codex"], timeout=1.0)

    monkeypatch.setattr(cr.subprocess, "run", fake_run)

    before = set(tmp_path.iterdir())
    with pytest.raises(RuntimeError, match="timed out"):
        cr.call_codex("sys", "user", timeout=1.0)
    after = set(tmp_path.iterdir())
    assert before == after, (
        f"call_codex leaked tempfile on timeout path: {after - before}"
    )

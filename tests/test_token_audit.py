"""File: tests/test_token_audit.py

Purpose: Coverage for scripts/token-audit.py — budget enforcement,
tiktoken fallback, truncation-sentinel behaviour (via the script's
own logic), and graceful degradation when tiktoken is absent.

Invariants & gotchas:
  - The 'budget' assertion compares current tier-1 + meta-prompt
    totals against the committed baseline in
    Documentation/TOKEN_BASELINE.json. A size-only assertion (no
    20%/25% threshold here) — the reduction targets were achieved
    in the Sprint 6 compaction commit; subsequent growth is what
    this test guards against.

Last updated: Sprint 6 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TA_PATH = REPO_ROOT / "scripts" / "token-audit.py"
BASELINE = REPO_ROOT / "Documentation" / "TOKEN_BASELINE.json"


def _load_module(repo_root: Path):
    spec = importlib.util.spec_from_file_location(
        f"ta_{repo_root.name}", TA_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.REPO_ROOT = repo_root
    mod.BASELINE = repo_root / "Documentation" / "TOKEN_BASELINE.json"
    mod.OUTPUT = repo_root / "Documentation" / "TOKEN_AUDIT.md"
    return mod


def test_count_tokens_with_tiktoken_or_fallback():
    """count_tokens must return > 0 for any non-empty text, whether
    tiktoken is available or not."""
    mod = _load_module(REPO_ROOT)
    n = mod.count_tokens("hello world")
    assert n >= 1


def test_count_tokens_fallback_without_tiktoken(monkeypatch):
    """When the encoder is None (tiktoken unavailable), the fallback
    is deterministic chars/4."""
    mod = _load_module(REPO_ROOT)
    text = "a" * 400
    n = mod.count_tokens(text, encoder=None)
    assert n == 100  # exactly len(text) // 4


def test_audit_paths_sorts_descending_within_category(tmp_path):
    mod = _load_module(tmp_path)
    t1 = tmp_path / "a.md"
    t2 = tmp_path / "b.md"
    t1.write_text("short")
    t2.write_text("x" * 4000)  # larger file
    samples = mod.audit_paths([t1, t2], [], encoder=None)
    assert samples[0].path == "b.md"
    assert samples[1].path == "a.md"


def test_tier1_total_within_baseline():
    """The current tier-1 total must stay at or below the committed
    baseline. If you're legitimately adding context, bump the
    baseline via scripts/token-audit.py --update-baseline in the
    same commit."""
    mod = _load_module(REPO_ROOT)
    encoder = mod._load_tiktoken()
    samples = mod.audit_paths(
        mod._default_tier1_paths(),
        mod._default_meta_prompt_paths(),
        encoder=encoder,
    )
    tier1 = sum(s.tokens for s in samples if s.category == "tier1")
    if not BASELINE.is_file():
        pytest.skip("no baseline committed yet")
    baseline = json.loads(BASELINE.read_text())
    base_tier1 = baseline.get("totals", {}).get("tier1")
    if not isinstance(base_tier1, int) or base_tier1 <= 0:
        pytest.skip("tier-1 baseline missing or zero")
    assert tier1 <= base_tier1, (
        f"Tier-1 token total {tier1} exceeds baseline {base_tier1}. "
        "Either trim the growth or run "
        "`python3 scripts/token-audit.py --update-baseline` to re-baseline."
    )


def test_each_meta_prompt_within_baseline():
    """Each meta-prompt stays at or below its per-file baseline
    entry. A new meta-prompt (no baseline entry) is allowed."""
    mod = _load_module(REPO_ROOT)
    encoder = mod._load_tiktoken()
    samples = mod.audit_paths(
        mod._default_tier1_paths(),
        mod._default_meta_prompt_paths(),
        encoder=encoder,
    )
    if not BASELINE.is_file():
        pytest.skip("no baseline committed yet")
    baseline = json.loads(BASELINE.read_text())
    files = baseline.get("files", {})
    bloats: list[str] = []
    for s in samples:
        if s.category != "meta_prompt":
            continue
        base = files.get(s.path)
        if not isinstance(base, int):
            continue  # new file; allowed
        if s.tokens > base:
            bloats.append(f"{s.path}: {s.tokens} > baseline {base}")
    assert not bloats, "Meta-prompt bloat detected:\n  " + "\n  ".join(bloats)


def test_build_report_empty_baseline(tmp_path):
    mod = _load_module(tmp_path)
    body = mod.build_report([], {}, tiktoken_available=True)
    assert "Token Audit" in body
    assert "Tier-1" in body


def test_build_report_notes_missing_tiktoken(tmp_path):
    mod = _load_module(tmp_path)
    body = mod.build_report([], {}, tiktoken_available=False)
    assert "tiktoken unavailable" in body


def test_read_text_safe_handles_non_utf8(tmp_path):
    """R1 #23: a file with invalid UTF-8 bytes must not crash the
    audit; _read_text_safe returns a replacement-decoded string and
    count_tokens falls back to the character count."""
    mod = _load_module(tmp_path)
    path = tmp_path / "binary.md"
    path.write_bytes(b"good \xff\xfe\xfd bad\n")
    text = mod._read_text_safe(path)
    assert "good" in text  # the valid portion survived
    n = mod.count_tokens(text, encoder=None)
    assert n >= 1


def test_check_headers_passes_after_compaction():
    """R1 #23: Sprint 6 moved prose from CLAUDE.md to
    Documentation/conventions.md. Both files must still satisfy the
    header convention — or be excluded from the check. A regression
    that re-inlined the prose wouldn't break pytest but would break
    check-headers. This test runs check-headers on the real repo to
    guard against that."""
    import subprocess as _sp
    result = _sp.run(
        [sys.executable, "scripts/check-headers.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"check-headers failed after compaction:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_subprocess_smoke(tmp_path):
    """End-to-end run writes TOKEN_AUDIT.md and exits 0."""
    # Mirror a minimal repo tree so the script has something to scan.
    (tmp_path / "scripts" / "bootstrap").mkdir(parents=True)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / "Documentation").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# claude md\n")
    (tmp_path / ".claude" / "skills" / "a.md").write_text("skill a\n")
    (tmp_path / "scripts" / "bootstrap" / "p.md").write_text("prompt p\n")
    (tmp_path / "scripts" / "token-audit.py").write_text(TA_PATH.read_text())
    result = subprocess.run(
        [sys.executable, "scripts/token-audit.py"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "Documentation" / "TOKEN_AUDIT.md").is_file()

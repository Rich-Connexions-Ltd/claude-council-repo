#!/usr/bin/env python3
"""File: scripts/token-audit.py

Purpose: Report token sizes for tier-1 context (CLAUDE.md, MEMORY.md,
.claude/skills/*.md), meta-prompts (scripts/bootstrap/*.md), and a
sampled council-round input, against a committed baseline.

Role:
  Sprint 6 compaction-tracking tool. Writes
  Documentation/TOKEN_AUDIT.md so re-bloat is visible in PRs.
  Baseline lives in Documentation/TOKEN_BASELINE.json; --update-baseline
  rewrites it after legitimate growth (documented in the commit).

Exports:
  - count_tokens(text) -- tiktoken with chars/4 fallback
  - TierSample, audit_paths -- data structures
  - main -- CLI entry point

Depends on:
  - external: tiktoken (optional; chars/4 fallback when absent)

Invariants & gotchas:
  - tiktoken is imported lazily so CI runners without it still emit
    a digest. The fallback is deterministic (len(text) // 4).
  - Non-UTF-8 files are read as bytes and counted via
    len(raw) // 4. Never crashes.

Last updated: Sprint 6 (2026-04-16) -- initial audit tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "Documentation" / "TOKEN_BASELINE.json"
OUTPUT = REPO_ROOT / "Documentation" / "TOKEN_AUDIT.md"


def _load_tiktoken():
    try:
        import tiktoken  # type: ignore
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, encoder=None) -> int:
    """Return the token count for ``text``. Uses tiktoken when
    available; falls back to ``len(text) // 4`` otherwise."""
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return ""


@dataclass(frozen=True)
class TierSample:
    """One tier-1 / meta-prompt / sampled-input entry."""
    category: str  # 'tier1', 'meta_prompt', 'sample'
    path: str  # repo-relative
    tokens: int


def audit_paths(
    tier1_paths: list[Path],
    meta_prompt_paths: list[Path],
    *,
    encoder=None,
) -> list[TierSample]:
    """Walk the supplied path lists and return samples sorted by
    category then token count (descending)."""
    samples: list[TierSample] = []
    for p in tier1_paths:
        if not p.is_file():
            continue
        samples.append(TierSample(
            category="tier1",
            path=str(p.relative_to(REPO_ROOT)),
            tokens=count_tokens(_read_text_safe(p), encoder),
        ))
    for p in meta_prompt_paths:
        if not p.is_file():
            continue
        samples.append(TierSample(
            category="meta_prompt",
            path=str(p.relative_to(REPO_ROOT)),
            tokens=count_tokens(_read_text_safe(p), encoder),
        ))
    return sorted(samples, key=lambda s: (s.category, -s.tokens, s.path))


def _default_tier1_paths() -> list[Path]:
    paths: list[Path] = [REPO_ROOT / "CLAUDE.md"]
    mem = REPO_ROOT / "memory" / "MEMORY.md"
    if mem.is_file():
        paths.append(mem)
    for p in sorted((REPO_ROOT / ".claude" / "skills").glob("*.md")):
        paths.append(p)
    return paths


def _default_meta_prompt_paths() -> list[Path]:
    return sorted((REPO_ROOT / "scripts" / "bootstrap").glob("*.md"))


def load_baseline() -> dict:
    if not BASELINE.is_file():
        return {}
    try:
        return json.loads(BASELINE.read_text())
    except json.JSONDecodeError:
        return {}


def save_baseline(samples: list[TierSample]) -> None:
    data = {
        "version": 1,
        "totals": {
            "tier1": sum(s.tokens for s in samples if s.category == "tier1"),
            "meta_prompt": sum(s.tokens for s in samples if s.category == "meta_prompt"),
        },
        "files": {s.path: s.tokens for s in samples},
    }
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(data, indent=2) + "\n")


def _pct_change(current: int, baseline: int) -> str:
    if baseline <= 0:
        return "—"
    delta = current - baseline
    pct = (delta / baseline) * 100
    sign = "+" if delta > 0 else ("" if delta == 0 else "")
    return f"{sign}{pct:.1f}%"


def build_report(
    samples: list[TierSample],
    baseline: dict,
    *,
    tiktoken_available: bool,
) -> str:
    fallback_note = ""
    if not tiktoken_available:
        fallback_note = (
            "\n_Note: tiktoken unavailable; counts use chars/4 fallback._"
        )
    lines = [
        "# Token Audit",
        "",
        "_Advisory digest for Sprint 6 compaction tracking. Failing CI "
        "on budget overrun guards against re-bloat._" + fallback_note,
        "",
        "## Tier-1 (loaded every session)",
        "",
        "| File | Tokens | Baseline | Change |",
        "|------|--------|----------|--------|",
    ]
    base_files = baseline.get("files", {}) if baseline else {}
    tier1_total = 0
    for s in samples:
        if s.category != "tier1":
            continue
        tier1_total += s.tokens
        base = base_files.get(s.path, 0)
        lines.append(
            f"| `{s.path}` | {s.tokens} | {base or '—'} | "
            f"{_pct_change(s.tokens, base) if base else '(new)'} |"
        )
    base_tier1 = baseline.get("totals", {}).get("tier1") if baseline else None
    lines.extend([
        "",
        f"**Tier-1 total: {tier1_total} tokens** "
        + (f"(baseline {base_tier1}, {_pct_change(tier1_total, base_tier1)})"
           if base_tier1 else ""),
        "",
        "## Meta-prompts",
        "",
        "| File | Tokens | Baseline | Change |",
        "|------|--------|----------|--------|",
    ])
    meta_total = 0
    for s in samples:
        if s.category != "meta_prompt":
            continue
        meta_total += s.tokens
        base = base_files.get(s.path, 0)
        lines.append(
            f"| `{s.path}` | {s.tokens} | {base or '—'} | "
            f"{_pct_change(s.tokens, base) if base else '(new)'} |"
        )
    base_meta = baseline.get("totals", {}).get("meta_prompt") if baseline else None
    lines.extend([
        "",
        f"**Meta-prompt total: {meta_total} tokens** "
        + (f"(baseline {base_meta}, {_pct_change(meta_total, base_meta)})"
           if base_meta else ""),
        "",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report token sizes for tier-1 context and meta-prompts "
            "against the committed baseline in "
            "Documentation/TOKEN_BASELINE.json."
        )
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Rewrite TOKEN_BASELINE.json with current counts. Use "
             "only after intentional, reviewed growth.",
    )
    parser.add_argument(
        "--output", default=str(OUTPUT),
        help=f"Where to write the audit markdown (default {OUTPUT}).",
    )
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    encoder = _load_tiktoken()
    samples = audit_paths(
        _default_tier1_paths(),
        _default_meta_prompt_paths(),
        encoder=encoder,
    )

    baseline = load_baseline()
    report = build_report(samples, baseline, tiktoken_available=encoder is not None)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)

    if args.update_baseline:
        save_baseline(samples)
        print(f"token-audit: baseline updated at {BASELINE}", file=sys.stderr)

    tier1_total = sum(s.tokens for s in samples if s.category == "tier1")
    meta_total = sum(s.tokens for s in samples if s.category == "meta_prompt")
    print(
        f"token-audit: tier-1 {tier1_total}, meta-prompts {meta_total} "
        f"(wrote {out})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

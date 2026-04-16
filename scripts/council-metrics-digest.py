#!/usr/bin/env python3
"""File: scripts/council-metrics-digest.py

Purpose: Read every council/metrics_Sprint*.jsonl (current and any
archived under Documentation/council-metrics-archive/) and write an
advisory digest to Documentation/COUNCIL_METRICS_DIGEST.md.

Role:
  Closes the observability loop for the Council of Experts pipeline.
  Reports rounds to convergence, verdict distribution, per-lens
  finding activity, reviewer success rate, and elapsed-time trend.
  Advisory only — never mutates council-config.json. Schema v2
  discriminates by a leading sentinel row; v1 files (no sentinel,
  no findings_by_lens) load with a compatibility notice.

Exports:
  - load_metrics_file -- parse one JSONL into (version, list[dict])
  - build_digest -- compose the markdown body from grouped rows
  - main -- CLI entry point

Depends on:
  - external: python stdlib only (json, sys, pathlib, datetime,
    collections, argparse)

Invariants & gotchas:
  - A schema sentinel has shape
    ``{"_schema": "council_metrics", "version": N}`` and MUST be the
    first parsed object. Data rows never carry ``_schema``.
  - ``members_active = 0`` or absent renders as ``"n/a"``; the
    success-rate division is never computed in that case.
  - Pre-v2 rows (no ``findings_by_lens``) render with a schema-v1
    notice rather than a zero-padded lens breakdown.

Last updated: Sprint 6 (2026-04-16) -- initial digest tool.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METRICS_DIR = REPO_ROOT / "council"
ARCHIVE_DIR = REPO_ROOT / "Documentation" / "council-metrics-archive"
OUTPUT = REPO_ROOT / "Documentation" / "COUNCIL_METRICS_DIGEST.md"

SPRINT_RE = re.compile(r"metrics_Sprint(?P<n>\d+)\.jsonl$")
SCHEMA_KEY = "_schema"
SCHEMA_VALUE = "council_metrics"
CURRENT_VERSION = 2


def _sprint_num(path: Path) -> int:
    m = SPRINT_RE.search(path.name)
    return int(m.group("n")) if m else -1


def load_metrics_file(path: Path) -> tuple[int, list[dict]]:
    """Parse a metrics JSONL. Returns (schema_version, rows).

    Files whose first parsed object carries the ``_schema`` sentinel
    load at that version. Files without a sentinel load as v1.
    Malformed rows are skipped with a stderr notice; the file is
    dropped entirely only if the first row is a malformed sentinel.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"metrics-digest: cannot read {path}: {exc}", file=sys.stderr)
        return (1, [])

    lines = [line for line in raw.splitlines() if line.strip()]
    rows: list[dict] = []
    version = 1
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"metrics-digest: skipping malformed line {i + 1} "
                f"in {path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        if i == 0 and isinstance(obj, dict) and SCHEMA_KEY in obj:
            if obj.get(SCHEMA_KEY) != SCHEMA_VALUE:
                print(
                    f"metrics-digest: rejecting {path.name}: "
                    f"unknown _schema value {obj.get(SCHEMA_KEY)!r}",
                    file=sys.stderr,
                )
                return (0, [])
            ver = obj.get("version")
            if isinstance(ver, int) and ver >= 1:
                version = ver
            continue
        if not isinstance(obj, dict):
            continue
        rows.append(obj)
    return version, rows


def _collect_inputs() -> list[Path]:
    """Collect all metrics files across current + archive dirs,
    deduplicated by sprint number. Archive wins on duplicates (it's
    the post-archival snapshot)."""
    inputs: list[Path] = []
    seen: set[int] = set()
    if ARCHIVE_DIR.exists():
        for p in sorted(ARCHIVE_DIR.glob("metrics_Sprint*.jsonl"), key=_sprint_num):
            n = _sprint_num(p)
            if n not in seen:
                inputs.append(p)
                seen.add(n)
    if METRICS_DIR.exists():
        for p in sorted(METRICS_DIR.glob("metrics_Sprint*.jsonl"), key=_sprint_num):
            n = _sprint_num(p)
            if n not in seen:
                inputs.append(p)
                seen.add(n)
    return inputs


def _success_rate(row: dict) -> str:
    active = row.get("members_active")
    succeeded = row.get("members_succeeded")
    if not isinstance(active, int) or active == 0:
        return "n/a"
    if not isinstance(succeeded, int):
        return "n/a"
    ratio = max(0.0, min(1.0, succeeded / active))
    return f"{ratio:.0%}"


def _rounds_summary(rows: list[dict]) -> tuple[int | None, int | None]:
    plan_rounds: list[int] = []
    code_rounds: list[int] = []
    for r in rows:
        rt = r.get("review_type")
        rn = r.get("round")
        if not isinstance(rn, int):
            continue
        if rt == "plan":
            plan_rounds.append(rn)
        elif rt == "code":
            code_rounds.append(rn)
    return (
        max(plan_rounds) if plan_rounds else None,
        max(code_rounds) if code_rounds else None,
    )


def _lens_activity(rows: list[dict], version: int) -> str:
    """Sprint 6 R1 #16: each row's ``findings_by_lens`` is a
    *cumulative* snapshot of the tracker at that round, not a per-round
    delta. Summing across rounds double-counts persistent findings.
    We therefore take the lens counts from the LAST row that carries
    the field — that's the final state of the sprint."""
    if version < 2:
        return "(schema v1 — no lens breakdown)"
    latest: dict[str, int] | None = None
    for r in rows:
        lens_counts = r.get("findings_by_lens")
        if isinstance(lens_counts, dict):
            latest = {
                str(k): v for k, v in lens_counts.items()
                if isinstance(v, int)
            }
    if latest is None:
        return "(schema v1 — no lens breakdown)"
    if not latest:
        return "0 findings"
    parts = sorted(latest.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{lens}:{n}" for lens, n in parts)


def _verdict_distribution(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        v = r.get("verdict") or "UNKNOWN"
        counts[str(v)] += 1
    if not counts:
        return "_(no verdicts)_"
    parts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{v}:{n}" for v, n in parts)


def _elapsed_average(rows: list[dict]) -> str:
    seconds = [r["elapsed_seconds"] for r in rows
               if isinstance(r.get("elapsed_seconds"), (int, float))]
    if not seconds:
        return "n/a"
    return f"{sum(seconds) / len(seconds):.1f}s avg"


def _security_bypasses(rows: list[dict]) -> int:
    return sum(1 for r in rows if r.get("security_bypassed") is True)


def build_digest(by_sprint: dict[int, tuple[int, list[dict]]]) -> str:
    """Compose the markdown digest from a {sprint_num: (version, rows)}
    mapping. Output is deterministic modulo the timestamp line."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Council Metrics Digest",
        "",
        f"_Generated {timestamp}. Advisory only — no config mutation._",
        "",
        "## Per-sprint summary",
        "",
        "| Sprint | Version | Plan rounds | Code rounds | Success rate | Elapsed | Verdicts | Lens activity | Security bypassed |",
        "|--------|---------|-------------|-------------|--------------|---------|----------|---------------|-------------------|",
    ]
    if not by_sprint:
        lines.extend(["", "_None yet._", ""])
        return "\n".join(lines) + "\n"

    for sprint in sorted(by_sprint):
        version, rows = by_sprint[sprint]
        plan_r, code_r = _rounds_summary(rows)
        success = "n/a"
        # Prefer the latest code row's success rate; fall back to plan.
        for r in reversed(rows):
            if _success_rate(r) != "n/a":
                success = _success_rate(r)
                break
        lines.append(
            f"| {sprint} | v{version} | "
            f"{plan_r if plan_r is not None else '—'} | "
            f"{code_r if code_r is not None else '—'} | "
            f"{success} | {_elapsed_average(rows)} | "
            f"{_verdict_distribution(rows)} | "
            f"{_lens_activity(rows, version)} | "
            f"{_security_bypasses(rows)} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate council/metrics_Sprint*.jsonl into an advisory "
            "digest at Documentation/COUNCIL_METRICS_DIGEST.md."
        )
    )
    parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    by_sprint: dict[int, tuple[int, list[dict]]] = {}
    for path in _collect_inputs():
        version, rows = load_metrics_file(path)
        sprint = _sprint_num(path)
        if sprint < 0:
            continue
        by_sprint[sprint] = (version, rows)

    digest = build_digest(by_sprint)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(digest)
    print(
        f"council-metrics-digest: wrote {OUTPUT} ({len(by_sprint)} sprint(s))",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

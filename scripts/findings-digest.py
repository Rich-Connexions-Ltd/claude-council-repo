#!/usr/bin/env python3
"""File: scripts/findings-digest.py

Purpose: Read all archived and current FINDINGS_Sprint<N>.md tracker
files, group findings by (lens, tag), and emit an advisory digest at
Documentation/FINDINGS_DIGEST.md. Read-only; does not mutate
council-config.json or any other state.

Role:
  Cross-sprint pattern mining. Surfaces themes appearing in multiple
  sprints and "resolved-but-recurring" findings (addressed/verified
  then reappeared). Input is the JSON-in-markdown tracker written by
  council-review.py with the lens/tag schema.

Exports:
  - main() -- CLI entrypoint

Depends on:
  - external: python stdlib only (json, re, pathlib, sys, datetime)

Invariants & gotchas:
  - Body output is deterministic: stable sort by (count desc, lens
    asc, tag asc). Only the top timestamp differs between runs.
  - Malformed file -> skip + warn on stderr; other files continue.
  - Missing Documentation/ -> created.

Last updated: Sprint 1 (2026-04-15) -- initial implementation
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "Documentation" / "findings-archive"
OUTPUT = REPO_ROOT / "Documentation" / "FINDINGS_DIGEST.md"

SPRINT_RE = re.compile(r"FINDINGS_Sprint(?P<n>\d+)\.md$")


def _sprint_num(path: Path) -> int:
    m = SPRINT_RE.search(path.name)
    return int(m.group("n")) if m else -1


def _parse_tracker(path: Path) -> list[dict]:
    """Parse the markdown-table tracker written by council-review.py.

    Schema v2 (8 cols): # | Round | Severity | Lens | Tag | Finding | Status | Resolution
    Schema v1 (6 cols, pre-Sprint 1): no Lens/Tag; read with defaults.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"findings-digest: cannot read {path}: {exc}", file=sys.stderr)
        return []
    findings: list[dict] = []
    in_table = False
    header_cols: list[str] = []
    for line in text.splitlines():
        if line.startswith("| #"):
            in_table = True
            header_cols = [c.strip().lower() for c in line.split("|")[1:-1]]
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) < 6:
                continue
            col = {name: (parts[i] if i < len(parts) else "") for i, name in enumerate(header_cols)}
            findings.append({
                "lens": col.get("lens", "unknown") or "unknown",
                "tag": col.get("tag", "untagged") or "untagged",
                "severity": col.get("severity", ""),
                "title": col.get("finding", ""),
                "status": (col.get("status") or "OPEN").upper(),
            })
        elif in_table and not line.startswith("|"):
            in_table = False
    if not findings:
        print(f"findings-digest: no findings table in {path}; skipping", file=sys.stderr)
    return findings


def _collect_inputs() -> list[Path]:
    """Collect archived + live sprint findings, deduplicated by sprint number.

    If the same sprint file exists in both Documentation/findings-archive/
    and the repo root (e.g. after a partial archive failure), the archive
    copy wins — it is the durable, already-reviewed version.
    """
    inputs: list[Path] = []
    seen: set[int] = set()
    if ARCHIVE_DIR.exists():
        for p in sorted(ARCHIVE_DIR.glob("FINDINGS_Sprint*.md"), key=_sprint_num):
            n = _sprint_num(p)
            if n not in seen:
                inputs.append(p)
                seen.add(n)
    for p in sorted(REPO_ROOT.glob("FINDINGS_Sprint*.md"), key=_sprint_num):
        n = _sprint_num(p)
        if n not in seen:
            inputs.append(p)
            seen.add(n)
    return inputs


def _group_findings(inputs: list[Path]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in inputs:
        sprint = _sprint_num(path)
        for f in _parse_tracker(path):
            lens = f.get("lens") or "unknown"
            tag = f.get("tag") or "untagged"
            status = (f.get("status") or "OPEN").upper()
            title = f.get("title") or ""
            severity = f.get("severity") or ""
            groups[(lens, tag)].append(
                {
                    "sprint": sprint,
                    "status": status,
                    "title": title,
                    "severity": severity,
                }
            )
    return groups


def _is_recurrence(entries: list[dict]) -> bool:
    """A (lens,tag) is 'resolved-but-recurring' if it was ADDRESSED or
    VERIFIED in some sprint N and then OPEN again in sprint M > N.
    WONTFIX does not count as resolved for this purpose.
    """
    by_sprint = sorted(entries, key=lambda e: e["sprint"])
    was_resolved = False
    resolved_at = -1
    for e in by_sprint:
        if e["status"] in {"ADDRESSED", "VERIFIED"}:
            was_resolved = True
            resolved_at = e["sprint"]
        elif e["status"] == "OPEN" and was_resolved and e["sprint"] > resolved_at:
            return True
    return False


def _format_group(key: tuple[str, str], entries: list[dict]) -> str:
    lens, tag = key
    sprints = sorted({e["sprint"] for e in entries})
    statuses = sorted({e["status"] for e in entries})
    title_sample = next((e["title"] for e in entries if e["title"]), "(no title)")
    return (
        f"- **{lens} / `{tag}`** — {len(entries)} finding(s) across "
        f"sprints {sprints}; statuses {statuses}. "
        f"Sample: _{title_sample.strip()[:120]}_"
    )


def build_digest(groups: dict[tuple[str, str], list[dict]]) -> str:
    ordered = sorted(
        groups.items(),
        key=lambda kv: (-len({e["sprint"] for e in kv[1]}), kv[0][0], kv[0][1]),
    )

    actionable: list[str] = []
    watchlist: list[str] = []
    recurring: list[str] = []

    for key, entries in ordered:
        sprint_count = len({e["sprint"] for e in entries})
        line = _format_group(key, entries)
        if _is_recurrence(entries):
            recurring.append(line)
        if sprint_count >= 3:
            actionable.append(line)
        elif sprint_count == 2:
            watchlist.append(line)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Findings Digest",
        "",
        f"_Generated {timestamp}. Advisory only — no config mutation._",
        "",
        "## Themes appearing in ≥3 sprints (actionable)",
        "",
    ]
    lines.extend(actionable or ["_None yet._"])
    lines.extend(["", "## Themes appearing in 2 sprints (watchlist)", ""])
    lines.extend(watchlist or ["_None yet._"])
    lines.extend(["", "## Resolved-but-recurring (highest signal)", ""])
    lines.extend(recurring or ["_None yet._"])
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    inputs = _collect_inputs()
    groups = _group_findings(inputs)
    digest = build_digest(groups)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(digest)
    print(f"findings-digest: wrote {OUTPUT} ({len(groups)} unique (lens,tag) groups)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

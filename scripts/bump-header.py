#!/usr/bin/env python3
"""File: scripts/bump-header.py

Purpose: Auto-update the Last updated line in a source file's header block
after a Claude Code Edit/Write operation, so headers stay in sync with the
current sprint and today's date without manual maintenance.

Role:
  PostToolUse hook script. Claude Code invokes this after every successful
  Edit/Write/MultiEdit, passing the modified file path as the first argument.
  The script reads the current sprint from .current-sprint (or defaults to
  HEAD), reads the file, finds the Last updated line within the first 150
  lines, and rewrites it to "Sprint <N> (<today>) -- edited". Silently
  no-ops if the file has no header, is excluded from header lint, or if
  the current Last updated already names the current sprint.

Exports:
  - main() -- CLI entry point, returns 0 on success or no-op

Depends on:
  - internal: scripts/check-headers.py (reuses EXCLUDED_DIRS, EXCLUDED_PREFIXES,
    LAST_UPDATED_RE, is_source_file, is_excluded, HEADER_SCAN_LINES)
  - external: none (stdlib only)

Invariants & gotchas:
  - MUST NOT fail noisily. A bad hook blocks every Edit across the repo.
    All error paths return 0 and log to stderr only.
  - MUST NOT modify non-source files (Markdown, JSON, HTML, etc.) or files
    excluded from check-headers.py scope.
  - Only rewrites the EXISTING Last updated line. Does not add a header to a
    file that lacks one -- that's the editor's job per CLAUDE.md.
  - Reads the current sprint from a .current-sprint file at repo root (one
    line, e.g. "124"); falls back to the highest Sprint N in CHANGES.md;
    falls back to "HEAD" if neither is available.
  - If the Last updated already advertises the current sprint, no write
    happens -- avoids touching mtimes and creating diff noise.
  - The commit message in the rewritten line is always "edited" to avoid
    overwriting more descriptive messages written by Claude during Edit.
    At sprint end, the Complete command will replace "edited" with a real
    summary.

Related:
  - scripts/check-headers.py -- the lint this keeps passing
  - CLAUDE.md "File Header Blocks" -- the template this maintains

Last updated: Sprint 124 (2026-04-13) -- initial hook script
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Reuse check-headers.py config via importlib (hyphenated filename).
_spec = importlib.util.spec_from_file_location(
    "check_headers", REPO_ROOT / "scripts" / "check-headers.py"
)
if _spec is None or _spec.loader is None:
    sys.exit(0)  # fail-open
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)


def _current_sprint() -> str:
    """Return the active sprint number (string like '124' or '68c')."""
    sprint_file = REPO_ROOT / ".current-sprint"
    if sprint_file.exists():
        value = sprint_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+[a-z]?", value):
            return value
    # Fall back to the highest sprint number in CHANGES.md.
    changes = REPO_ROOT / "CHANGES.md"
    if changes.exists():
        sprints = re.findall(r"Sprint\s+(\d+[a-z]?)", changes.read_text(encoding="utf-8"))
        if sprints:
            # Sort by numeric then letter suffix.
            def _key(s: str) -> tuple[int, str]:
                m = re.match(r"(\d+)([a-z]?)", s)
                return (int(m.group(1)), m.group(2)) if m else (0, "")
            return sorted(sprints, key=_key)[-1]
    return "HEAD"


def _bump(path: Path, sprint: str, today: str) -> bool:
    """Rewrite the Last updated line if stale. Returns True if rewritten."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    # Only scan the first HEADER_SCAN_LINES lines for the Last updated line.
    lines = content.splitlines(keepends=True)
    scan_limit = min(_check.HEADER_SCAN_LINES, len(lines))
    head = "".join(lines[:scan_limit])
    match = _check.LAST_UPDATED_RE.search(head)
    if match is None:
        return False  # no header -- not our job to add one

    if match.group("sprint") == sprint and match.group("date") == today:
        return False  # already current, no-op

    # Build the replacement line, preserving surrounding whitespace/indent.
    old_line = match.group(0)
    new_line = f"Last updated: Sprint {sprint} ({today}) -- edited"
    # Preserve the leading portion of the matched line (e.g. " * " or "# ")
    # by finding the line containing the match.
    # Locate the line index within `head`.
    prefix_idx = head.rfind("\n", 0, match.start()) + 1
    line_end = head.find("\n", match.end())
    if line_end == -1:
        line_end = len(head)
    original_line = head[prefix_idx:line_end]
    # The prefix is everything before "Last updated:" on the matched line.
    lead = original_line[: original_line.find("Last updated:")]
    replacement_line = f"{lead}{new_line}"

    new_content = content[:prefix_idx] + replacement_line + content[prefix_idx + len(original_line):]
    if new_content == content:
        return False
    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError:
        return False
    return True


def main() -> int:
    # Argv layout when invoked as a PostToolUse hook: the modified file path
    # is passed as $1. Defensive: accept any extra args.
    if len(sys.argv) < 2:
        return 0
    candidate = sys.argv[1]
    if not candidate:
        return 0

    path = Path(candidate)
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        path = path.resolve()
    except OSError:
        return 0

    # Only act on files under REPO_ROOT.
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return 0

    if not path.is_file():
        return 0
    if not _check.is_source_file(path):
        return 0
    if _check.is_excluded(path):
        return 0

    sprint = _current_sprint()
    today = _dt.date.today().isoformat()
    _bump(path, sprint, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())

---
name: check-headers
description: Audit file-header convention compliance across the repo. Lints every source file for the Purpose/Role/Exports/Depends/Invariants/Last-updated block, and flags stale Last-updated lines that predate the current sprint. Invoke before archiving a sprint or when headers drift.
---

# Check headers

This skill wraps `python3 scripts/check-headers.py`.

## Common invocations

```
# Full audit
python3 scripts/check-headers.py

# Only changed files (vs a git ref)
python3 scripts/check-headers.py --changed-against main

# Flag stale Last-updated lines for a specific sprint
python3 scripts/check-headers.py --sprint <N>
```

## What it checks

- `File:` line present and matches the actual path.
- `Purpose:` line present and non-empty.
- `Last updated:` line present with format `Sprint <N> (YYYY-MM-DD)`.
- `--sprint <N>` flag: warns on files touched in this sprint whose `Last updated` is older than N.

## Automation

The PostToolUse hook `scripts/bump-header.py` rewrites the `Last updated` line automatically on every Write/Edit. This skill is the manual audit surface.

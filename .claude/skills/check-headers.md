---
name: check-headers
description: Lint file-header blocks and flag stale Last-updated lines. Run before archiving a sprint.
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

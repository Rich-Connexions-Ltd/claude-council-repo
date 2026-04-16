---
name: sprint-start
description: Start a sprint in the Claude-Council workflow. Reads SPRINTS.md for the current sprint's goal and deliverables, runs the codegraph incremental index, pulls a context pack for the key files, and drafts PLAN_Sprint<N>.md with the mandatory Test Strategy section. Invoke when the user says "Sprint N" or asks to begin a new sprint.
---

# Sprint start

This skill orchestrates Phase 1 of the sprint process.

## Steps

1. Read `SPRINTS.md` and locate the Sprint N entry. Extract goal, deliverables, exit criteria.
2. Run `python3 scripts/index-codebase.py --incremental` to refresh the codegraph.
3. Identify the key files the sprint will touch, then run `python3 scripts/index-codebase.py --context-for <files...>` to pull a context pack.
4. Draft `PLAN_Sprint<N>.md` with the sections required by `CLAUDE.md`:
   - Problem statement + spec references
   - Current State (derived from codegraph queries, not file reads)
   - Proposed solution + alternatives considered
   - Component-by-component design with file paths
   - **Test Strategy** (mandatory, 5 subsections — property/invariant, failure-path, regression guards, fixture reuse, runtime budget)
   - Risks and mitigations
5. Ask the user to review the draft, then request council plan review via the `council-plan` skill.

## Notes

- The codegraph is the primary context source. Query it before reading source files.
- Every finding resolved in a prior sprint should have a named regression guard in the new plan's Test Strategy §3.

---
name: council-code
description: Request a Council of Experts code review. Run after implementing the approved plan, before archival.
---

# Council code review

This skill wraps `./scripts/council-review.py code <N> "<title>"`.

## Usage

```
./scripts/council-review.py code <N> "<title>"
```

One invocation = one round. Code review convergence guardrail caps at 6 rounds.

## When to use

- After completing implementation of an APPROVED plan, before `sprint-complete`.
- After revising code in response to a prior `CHANGES_REQUESTED` verdict.

## Verdicts

- `APPROVED` — proceed to archival.
- `CHANGES_REQUESTED` — address findings, re-run.
- `PLAN_REVISION_REQUIRED` — code review surfaced a problem the plan missed. Revise the plan, re-run `council-plan`, then re-run code review.

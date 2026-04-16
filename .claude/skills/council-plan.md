---
name: council-plan
description: Request a Council of Experts review of a sprint plan. Runs four parallel reviewers (security, code quality, test quality, domain) plus a cross-family consolidator against PLAN_Sprint<N>.md. Produces REVIEW_Sprint<N>.md with a verdict (APPROVED or CHANGES_REQUESTED) and FINDINGS_Sprint<N>.md with structured findings. Invoke after drafting or revising a plan.
---

# Council plan review

This skill wraps `./scripts/council-review.py plan <N> "<title>"`.

## Usage

```
./scripts/council-review.py plan <N> "<title>"
```

One invocation = one round. Re-run after addressing findings. The plan review convergence guardrail caps at 5 rounds; beyond that, the consolidator may force APPROVED.

## When to use

- Immediately after drafting `PLAN_Sprint<N>.md`.
- After revising the plan in response to a prior `CHANGES_REQUESTED` verdict.
- Not for code review — use the `council-code` skill for that.

## Reading the output

- `REVIEW_Sprint<N>.md` — human-readable consolidated review with verdict, design assessment, findings list.
- `FINDINGS_Sprint<N>.md` — structured tracker with status (OPEN/ADDRESSED/WONTFIX/VERIFIED), lens, tag, severity, round seen.
- Convergence metric at the end of each round indicates progress.

## Preflight

Run `./scripts/council-check.sh` if any reviewer reports UNAVAILABLE.

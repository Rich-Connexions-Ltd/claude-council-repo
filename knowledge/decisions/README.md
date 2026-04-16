# Decisions — {{PROJECT_NAME}}

*What lives here:* Architecture Decision Records (ADRs) —
significant, durable decisions about the system.
*What does not:* Implementation details (→ `architecture/`),
operational playbooks (→ `runbook/`), day-to-day domain rules
(→ `domain/`).
*See also:* [architecture](../architecture/README.md) ·
[domain](../domain/README.md) · [runbook](../runbook/README.md)

## ADR template

Copy to `NNNN-short-title.md` (zero-padded, monotonically
increasing):

```markdown
# ADR-NNNN: Short title

**Status:** proposed | accepted | deprecated | superseded by ADR-MMMM
**Date:** YYYY-MM-DD
**Deciders:** <names>

## Context

What is the issue we're seeing that is motivating this decision or
change? What constraints and forces are at play?

## Decision

What is the change we're making?

## Consequences

What becomes easier or harder as a result? What trade-offs are we
accepting? What will we monitor to know if this was the right call?

## Alternatives considered

- **Option A**: <what we'd have done instead> — <why we didn't>.
- **Option B**: ...
```

## Conventions

- **Immutable once accepted.** If a decision changes, write a new
  ADR that supersedes the old one; don't edit the old file. This
  preserves the decision trail.
- Sprint plans (`Documentation/archive/PLAN_Sprint*.md`) are NOT
  ADRs — they're implementation plans. Promote a plan to an ADR
  when the decision has lasting consequences beyond the sprint.
- File names are `NNNN-kebab-case-short-title.md`. The number is
  monotonically increasing; once used, never reused.

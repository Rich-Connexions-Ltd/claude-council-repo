# Architecture — {{PROJECT_NAME}}

*What lives here:* System boundaries, services map, deployment
model, trust boundaries, data-flow diagrams.
*What does not:* Business-domain entities (→ `domain/`), runbooks
(→ `runbook/`), decisions (→ `decisions/`).
*See also:* [domain](../domain/README.md) ·
[runbook](../runbook/README.md) · [decisions](../decisions/README.md)

## Template entries

Copy one of these headings into a new file and fill in the body.
Keep each topic in its own file — `one-subject-per-file` is how the
Domain Expert council reviewer reads `knowledge/`.

- [ ] `system-overview.md` — What problem does this system solve,
      what are its external consumers, what are the top-level
      components?
- [ ] `service-boundaries.md` — Internal services (if > 1), their
      responsibilities, the protocol between them.
- [ ] `deployment.md` — How the system runs in production: cloud,
      clusters, regions, scaling dimensions, SLOs.
- [ ] `trust-boundaries.md` — Where untrusted input enters, where
      it's validated, where privileged operations happen.
- [ ] `data-flow.md` — How data moves through the system; sources,
      sinks, transformations, retention.

## Conventions

- Markdown files only. No header block needed (Markdown is excluded
  from `check-headers`).
- Every file opens with a one-line purpose statement followed by
  the answers to: what is this, why does it exist, where does it
  sit, what invariants or gotchas matter.
- Cross-link with relative Markdown links; `[AID](../domain/keri.md#aids)`.
- When a diagram helps, embed it as Mermaid in a fenced block.

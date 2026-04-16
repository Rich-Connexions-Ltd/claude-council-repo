# Runbook — {{PROJECT_NAME}}

*What lives here:* Operational playbooks — what to do when X
happens. Oncall procedures, deploy + rollback, incident response.
*What does not:* Architecture diagrams (→ `architecture/`), domain
rules (→ `domain/`), decisions (→ `decisions/`).
*See also:* [architecture](../architecture/README.md) ·
[domain](../domain/README.md) · [decisions](../decisions/README.md)

## Template entries

- [ ] `deploy.md` — How to deploy a new version: PR → merge → CI →
      prod. Smoke checks. Rollback procedure.
- [ ] `oncall.md` — Oncall rotation, paging policy, SLO targets,
      where to find dashboards.
- [ ] `incident-response.md` — Severity matrix, who to page,
      communication template, post-incident review process.
- [ ] `data-recovery.md` — Backup retention, restore procedure,
      partial-data-loss recovery steps.
- [ ] `migrations.md` — How to run a schema migration safely in
      production; the two-phase pattern for NOT NULL columns.

## Conventions

- Procedures are step-numbered. Every step states what to check
  before moving on.
- Every runbook page names its audience at the top ("Who this is
  for: oncall engineer paged at 3am").
- Commands in fenced code blocks; copy-pasteable verbatim.
- When a runbook references a dashboard / alert, link it by URL.

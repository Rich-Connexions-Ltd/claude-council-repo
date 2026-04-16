# Domain — {{PROJECT_NAME}}

*What lives here:* Business-domain entities, terminology, state
machines, invariants specific to the problem area.
*What does not:* Technical architecture (→ `architecture/`),
operational playbooks (→ `runbook/`), decisions (→ `decisions/`).
*See also:* [architecture](../architecture/README.md) ·
[runbook](../runbook/README.md) · [decisions](../decisions/README.md)

## Template entries

Copy one of these headings into a new file and fill in the body.
One subject per file.

- [ ] `glossary.md` — Domain terms with crisp one-line definitions.
      Every piece of jargon a new engineer asks about belongs here.
- [ ] `entities.md` — Primary data models / aggregates + their
      relationships. State machines for entities that have
      non-trivial lifecycles.
- [ ] `rules.md` — Business rules and invariants: validation
      thresholds, regulatory constraints, money-handling
      conventions.
- [ ] `flows.md` — User- or system-level flows (signup, checkout,
      incident-response kickoff) stated as step sequences.

## Conventions

- Markdown. No source-file header needed.
- Domain files are the primary reference for the Domain Expert
  council reviewer. Content here is citable as evidence; content
  invented without a source document is hallucination and must be
  flagged as such.
- Regulatory or normative text should be quoted verbatim, not
  paraphrased, and the source cited by URL + retrieval date.

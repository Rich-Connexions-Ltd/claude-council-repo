# {{PROJECT_NAME}} — Claude Code Instructions

> Filled in by `scripts/bootstrap.py`. Replace `{{PROJECT_NAME}}`,
> `{{MVP_OUTCOME}}`, and any other placeholders. This top block is
> descriptive; the rest is the working convention.

> **Developing the template itself?** You're likely in the
> `claude-council-dev` dev-container. See
> `Documentation/DEV_CONTAINER.md` for the split-repo model.

## Project overview

**MVP outcome:** {{MVP_OUTCOME}}

**Stack:** {{STACK}}

{{PROFILE_NOTE}}

## Install profile

Profiles gate which components ship: `minimal` (headers + codegraph
only), `standard` (adds council, skills, guardrails, compaction
hints, findings archive), `full` (adds digest + metrics-digest).

Source of truth: `scripts/bootstrap/profiles.json`. Current profile:
`.claude/project-profile`. Consumer API: `scripts/profile.py`
(`is_enabled(component)`).

## Knowledge base

### Tier 0: Codegraph — QUERY FIRST, READ SECOND (mandatory)

Before reading any source file to understand structure, query
`.claude/codebase.db` via `scripts/index-codebase.py` or the
`codegraph_*` MCP tools. The codegraph is auto-updated after every
Write/Edit via a PostToolUse hook.

```bash
python3 scripts/index-codebase.py --context-for path/to/file.py
python3 scripts/index-codebase.py --query "SELECT * FROM endpoints"
python3 scripts/index-codebase.py --stats
python3 scripts/index-codebase.py --stale-exports
python3 scripts/index-codebase.py --incremental
```

Tables: `files`, `symbols`, `imports`, `endpoints`, `models`,
`model_fields`, `tests`, `file_headers`, `file_header_exports`,
`file_header_depends`.

### Tier 1: Always loaded

`CLAUDE.md` (this file), `MEMORY.md` (auto-memory index).

### Tier 2: Directory-scoped

Each top-level directory may have its own `CLAUDE.md`. Read when
working in that directory.

### Tier 3: Deep reference (`knowledge/`)

{{KNOWLEDGE_INDEX}} *(bootstrap fills this with one line per
`knowledge/*.md` file)*

## Sprint process

### "Sprint N"

When the user says "Sprint N":

1. Read `SPRINTS.md` for goal, deliverables, exit criteria.
2. `python3 scripts/index-codebase.py --incremental`.
3. `python3 scripts/index-codebase.py --context-for <key files>`.
4. Draft `PLAN_Sprint<N>.md` with: problem statement + spec refs;
   Current State from codegraph; proposed solution with alternatives;
   component-by-component design with file paths; **Test Strategy
   section (MANDATORY — see below)**; risks and mitigations.
5. Request plan review: `./scripts/council-review.py plan <N> "<title>"`.
6. Iterate on feedback until `APPROVED`.
7. Implement. **Commit before requesting code review** — reviewers
   diff against `.sprint-base-commit-<N>`, and untracked files are
   rejected. Code review:
   `./scripts/council-review.py code <N> "<title>"`.
   (Use `--allow-untracked` only for pre-commit approach-review.)
8. On APPROVED, archive: `./scripts/archive-plan.sh <N> "<title>"`.

### Mandatory Test Strategy section

Every `PLAN_Sprint<N>.md` MUST include `## Test Strategy` with five
subsections: **Property / invariant coverage**, **Failure-path
coverage**, **Regression guards** (one per prior-round finding),
**Fixture reuse plan**, **Test runtime budget** (+ flaky policy).

### "Complete"

1. Update `SPRINTS.md`.
2. Update `knowledge/` for anything that shifted.
3. `./scripts/check-headers.py --sprint <N>` and fix warnings.
4. Re-read touched files; update Purpose/Role/Exports/Depends
   /Invariants if they drifted; replace the `-- edited` placeholder
   on `Last updated`.
5. If `FINDINGS_Sprint<N>.md` exists, review for council-process
   inefficiencies and update `scripts/council-config.json` if needed.
   If `digest` is enabled, run `python3 scripts/findings-digest.py`.
6. Every finding has a non-OPEN status (ADDRESSED, WONTFIX, or
   VERIFIED) with a resolution note.
7. `./scripts/archive-plan.sh <N> "<title>"`.
8. Commit and push.

### "human review on" / "human review off"

Toggles whether the human sees PLAN + REVIEW summaries before the
Editor acts on the verdict. State: `memory/human-review-mode`.

## File headers

Every source file carries a structured header block with at least
`File`, `Purpose`, `Last updated`. Non-trivial code also needs
`Role` + `Exports`. The PostToolUse hook auto-bumps `Last updated`
after every Edit/Write. **Full template, language table, automation
layers**: `Documentation/conventions.md`.

## Council of Experts

Four parallel reviewers (Security / Code Quality / Test Quality /
Domain) + a consolidator. All are Claude CLI subprocesses with
live MCP codegraph access.

```bash
./scripts/council-review.py plan <N> "<title>"
./scripts/council-review.py code <N> "<title>"
```

Config: `scripts/council-config.json`. Guardrails: plan max 5
rounds, code max 6. Metrics: `council/metrics_Sprint<N>.jsonl`.

**Selective routing** (code reviews only):
`--lenses security,code_quality` narrows to a subset; `--auto-lenses`
picks based on the diff (security + code_quality always;
test_quality if `tests/` changed; domain if `knowledge/` changed).
`--allow-no-security` is required when `--lenses` omits security.

**Finding states**: `OPEN`, `ADDRESSED`, `VERIFIED`, `WONTFIX`,
`REOPENED`, `RECURRING`. See `Documentation/conventions.md` for the
state machine and the `RECURRING` auto-demotion rule.

**Council tuning**:
- When the diff is narrow — e.g. tests-only, a docs-only touch, a
  knowledge-base refresh — prefer `--auto-lenses` on the code
  review. Skipping irrelevant lenses cuts round time ~50% and keeps
  the metrics digest honest about which lens is doing the work.
- Use explicit `--lenses security,code_quality` only when you
  specifically want to silence a noisy lens for one round. Never
  omit `security` on code; the override requires `--allow-no-security`
  and is recorded as `security_bypassed: true` in metrics.
- `python3 scripts/council-metrics-digest.py` — advisory digest of
  rounds-to-convergence, lens activity, reviewer success rate,
  security-bypass count. Run before deciding whether a lens is
  pulling its weight.
- `python3 scripts/token-audit.py` — tier-1 + meta-prompt token
  sizes against baseline; fails CI on growth. Run with
  `--update-baseline` after intentional growth and commit the new
  `Documentation/TOKEN_BASELINE.json`.
- `--verbose` on `council-review.py` restores per-member timing and
  the full header block when debugging a hung or UNAVAILABLE
  reviewer. Default output is terse for cheap eyeballing of steady-
  state runs.

## Permissions (pre-authorised)

- `git`, `gh` — all operations.
- `./scripts/*` — all template scripts.
- `pytest`, `python3`, `pip3` — test + tooling.

Bootstrap permission model (detail: `Documentation/conventions.md`):
`claude -p` runs with `--permission-mode default` (asks before any
tool use) or `acceptEdits` for two summarisation steps that write
derived files. **`bypassPermissions` is never used.** User-supplied
inputs are wrapped in `<<<USER_INPUT_BEGIN>>>`/`END>>>` fences; Claude
outputs are AST-validated before any disk write.

## Auto-memory

`memory/MEMORY.md` is the index. Types: `user`, `feedback`, `project`,
`reference`. Full convention: the auto-memory section that Claude
Code surfaces automatically.

## Troubleshooting

See `Documentation/conventions.md` for the full table. Common:
`check-headers` warns → bump `Last updated`. Council UNAVAILABLE →
`./scripts/council-check.sh`. Codegraph stale → `--incremental`.
Token audit fails → trim or re-baseline.

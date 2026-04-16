# {{PROJECT_NAME}} — Claude Code Instructions

> This file is filled in by `scripts/bootstrap.py`. After bootstrap,
> replace `{{PROJECT_NAME}}`, `{{MVP_OUTCOME}}`, and any other
> placeholders. This top block is descriptive; everything below it is
> the working convention.

> **Developing the template itself?** You're likely in the
> `claude-council-dev` dev-container, not a user project. See
> `Documentation/DEV_CONTAINER.md` for the split-repo model.

## Project overview

**MVP outcome:** {{MVP_OUTCOME}}

**Stack:** {{STACK}}

{{PROFILE_NOTE}}

## Install profile

This project was bootstrapped with an install profile. Profiles
control which components ship: `minimal` (headers + codegraph only),
`standard` (adds council review, skills, guardrails, compaction hints,
findings archive), or `full` (adds advisory findings digest).

- Profile membership: `scripts/bootstrap/profiles.json` (source of
  truth).
- Current project's profile: `.claude/project-profile`.
- Consumer API: `scripts/profile.py` (Python `is_enabled(component)`
  and CLI `python3 scripts/profile.py is-enabled <component>`).
- Hooks wired: `.claude/settings.json` (PreToolUse guardrails on
  `standard`+, PostToolUse `bump-header` always).

## Knowledge base

This repo uses a tiered knowledge system for Claude Code context:

### Tier 0: Codegraph — QUERY FIRST, READ SECOND (mandatory)

Before reading any source file to understand structure, query
`.claude/codebase.db` via `scripts/index-codebase.py` or the
`codegraph_*` MCP tools. The codegraph is auto-updated after every
Write/Edit via a PostToolUse hook.

```bash
# Context pack for specific files
python3 scripts/index-codebase.py --context-for path/to/file1 path/to/file2

# Ad-hoc SQL
python3 scripts/index-codebase.py --query "SELECT * FROM endpoints WHERE path LIKE '%foo%'"

# Stats
python3 scripts/index-codebase.py --stats

# Drift checks
python3 scripts/index-codebase.py --stale-exports
python3 scripts/index-codebase.py --stale-depends
```

Tables: `files`, `symbols`, `imports`, `endpoints`, `models`,
`model_fields`, `tests`, `file_headers`, `file_header_exports`,
`file_header_depends`.

### Tier 1: Always loaded

- `CLAUDE.md` (this file) — instructions, conventions.
- `MEMORY.md` (auto-memory) — master index, user preferences, gotchas.

### Tier 2: Directory-scoped

Each top-level directory may have its own `CLAUDE.md` with subsystem
context. Read these when working in that directory.

### Tier 3: Deep reference (`knowledge/`)

{{KNOWLEDGE_INDEX}} *(bootstrap fills this with one line per
`knowledge/*.md` file)*

## Sprint process

### "Sprint N"

When the user says "Sprint N" (e.g. "Sprint 3"):

1. Read `SPRINTS.md` for the sprint's goal, deliverables, exit criteria.
2. Run `python3 scripts/index-codebase.py --incremental`.
3. Run `python3 scripts/index-codebase.py --context-for <key files>`.
4. Draft `PLAN_Sprint<N>.md` with:
   - Problem statement + spec references.
   - Current State section derived from codegraph queries.
   - Proposed solution with alternatives considered.
   - Component-by-component design with file paths.
   - **Test Strategy section** (MANDATORY — see below).
   - Risks and mitigations.
5. Request plan review: `./scripts/council-review.py plan <N> "<title>"`.
6. Iterate on feedback until `APPROVED`.
7. Implement. **Commit your implementation before requesting code
   review** — reviewers diff against `.sprint-base-commit-<N>`, and
   untracked files will be rejected by the pre-flight check. Request
   code review: `./scripts/council-review.py code <N> "<title>"`.
   (Use `--allow-untracked` only for pre-commit approach-review.)
8. On APPROVED, archive: `./scripts/archive-plan.sh <N> "<title>"`.

### Mandatory Test Strategy section

Every `PLAN_Sprint<N>.md` MUST include "## Test Strategy" with five
subsections:

1. **Property / invariant coverage** — what invariant does each test
   class enforce?
2. **Failure-path coverage** — what error classes, timeouts,
   cancellation, boundary conditions are tested?
3. **Regression guards** — for every finding resolved in a prior round,
   which test prevents recurrence?
4. **Fixture reuse plan** — what shared fixtures do tests use? How is
   test isolation maintained?
5. **Test runtime budget** — target total runtime; flaky-test policy.

### "Complete"

When the user says "Complete":

1. Update `SPRINTS.md` to reflect work completed.
2. Update `knowledge/` for anything that shifted (architecture,
   models, APIs).
3. Run `./scripts/check-headers.py --sprint <N>` and fix any warnings.
4. Re-read each touched file and re-read its header. Update
   Purpose/Role/Exports/Depends/Invariants if they drifted. Replace the
   "-- edited" placeholder on `Last updated` with a one-line summary.
5. If `FINDINGS_Sprint<N>.md` exists, review it for council-process
   inefficiencies (drip-fed findings, redundant rounds, lens gaps) and
   update `scripts/council-config.json` if needed. If the `digest`
   component is enabled (`python3 scripts/profile.py is-enabled
   digest`), run `python3 scripts/findings-digest.py` and consult
   `Documentation/FINDINGS_DIGEST.md` when deciding on tweaks.
6. Ensure every finding has a non-OPEN status (ADDRESSED, WONTFIX, or
   VERIFIED) with a resolution note.
7. Run `./scripts/archive-plan.sh <N> "<title>"`.
8. Commit and push.

### "human review on" / "human review off"

Toggle whether the human sees PLAN + REVIEW summaries before the Editor
acts on the verdict. State lives in `memory/human-review-mode`.

## File header convention

Every source file carries a structured header block at the top.
Template:

```
File: <repo-relative path>
Purpose: <one sentence — what this file is and why it exists>

Role:
  <2-4 sentences: where this sits in the system, what problem it solves>

Exports:
  - <Name> -- <one-line description>

Depends on:
  - internal: <module> (for <reason>)
  - external: <package> (for <reason>)

Invariants & gotchas:
  - <constraint that must be preserved>

Related:
  - <file or knowledge doc> -- <why it's relevant>

Last updated: Sprint <N> (<YYYY-MM-DD>) -- <one-line summary>
```

Required fields: `File`, `Purpose`, `Last updated`. Required on
non-trivial code files: `Role`, `Exports`. Optional when they add
value: `Depends on`, `Invariants & gotchas`, `Related`.

### Comment syntax per language

| Language | Block style |
|---|---|
| Python (`.py`) | Module docstring (`"""..."""`) |
| TypeScript / JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`) | `/** ... */` JSDoc |
| Shell (`.sh`), Dockerfile | `#` comment block |
| YAML (`.yml`, `.yaml`) | `#` comment block |
| SQL (`.sql`) | `--` comment block |
| Rust (`.rs`) | `//!` inner doc-comment block |
| Go (`.go`) | `//` line comments before `package` |
| Java (`.java`) | `/** ... */` Javadoc |
| Markdown, `knowledge/`, `Documentation/` | No header needed |

### Automation (three layers)

1. **PostToolUse hook** (`scripts/bump-header.py`) — rewrites the
   `Last updated` line to the current sprint and today's date after
   every Edit/Write.
2. **Sprint-end audit** — step 4 of the "Complete" command: re-read each
   touched file, update fields that drifted.
3. **Council reviewer check** — Code Quality lens includes an explicit
   header accuracy check.

## Council of Experts

Four parallel reviewers + one consolidator, all Claude CLI subprocesses
with live MCP access to the codegraph.

```bash
./scripts/council-review.py plan <N> "<title>"
./scripts/council-review.py code <N> "<title>"
```

Config: `scripts/council-config.json`. Convergence guardrails: plan max
5 rounds, code max 6 rounds. Each round emits metrics to
`council/metrics_Sprint<N>.jsonl`.

### Finding states

Findings move through a small state machine:

| State | Meaning |
|---|---|
| `OPEN` | Just raised; unaddressed. |
| `ADDRESSED` | Editor claims the issue is fixed; next round will verify. |
| `VERIFIED` | Reviewer confirmed the fix. |
| `WONTFIX` | Accepted as out-of-scope with a resolution note. |
| `REOPENED` | Editor marked ADDRESSED but a subsequent round re-raised it. |
| `RECURRING` | ADDRESSED and re-raised **3+ times** — the merge logic auto-demotes it to Known Debt ("oscillating"). RECURRING findings stop blocking APPROVED and surface in the convergence summary (`X/Y resolved, Z open, W reopened, R recurring`) so a human can triage them out-of-band. |

The oscillation demotion lives in `_merge_findings` (`scripts/council-review.py`). RECURRING is a terminal state — once tagged it stays tagged for the rest of the sprint.

## Permissions (pre-authorised)

- `git`, `gh` — all operations.
- `./scripts/*` — all template scripts.
- `pytest`, `python3`, `pip3` — test + tooling.

### Bootstrap permission model

`scripts/bootstrap.py` invokes `claude -p` with `--permission-mode
default` (Claude asks the user before any tool use) or — for the
two summarisation steps that need to write derived files —
`acceptEdits` (auto-approves Edit/Write tool invocations).
**`acceptEdits` is a UX mode, not a scope boundary**: Claude can
choose what to edit. The real security invariant is that
**`bypassPermissions` is never used**, the user is interactively
present, and the high-risk steps (`step2b_generate_other`,
`step4_sprints`) keep `allow_edits=False` and write through
bootstrap-owned validators.

User-provided inputs (project brief, language names, uploaded docs)
are wrapped in `<<<USER_INPUT_BEGIN>>>` / `<<<USER_INPUT_END>>>`
fences before being embedded in prompts; meta-prompts instruct
Claude to treat fenced content as data, not instructions. Inputs
containing the fence tokens are rejected at wizard time. Slug,
language, extension, and grammar fields are constrained to a safe
character class to prevent path traversal.

Claude outputs are validated before any disk write: generated
Python is AST-parsed and rejected on dangerous calls (`subprocess`,
`os.system`, `eval`, `exec`, `__import__`, `compile`,
`open(... mode='w')`); `SPRINTS.md` proposals must match a
markdown shape; patches are whitelist-restricted to known-target
files and reject rename/copy/mode-change headers. Every disk write
requires interactive user confirmation.

To audit bootstrap's subprocess calls before running:
`grep -n 'run_claude_cli\|subprocess.run' scripts/bootstrap.py`.

## Auto-memory

`memory/MEMORY.md` is the index. Memory types: `user`, `feedback`,
`project`, `reference`. See the auto-memory section that Claude Code
surfaces automatically — the convention is documented there.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check-headers` warns on a file you didn't touch | Probably stale `Last updated`. Bump it to current sprint and commit. |
| Council reviewer "UNAVAILABLE" | Run `./scripts/council-check.sh` — likely missing `claude login` or `GOOGLE_API_KEY`. |
| Codegraph query returns 0 rows for a newly-added file | Run `python3 scripts/index-codebase.py --incremental`. |
| Codegraph drift-check (`--stale-exports`) flags legitimate exports | The symbol exists but is re-exported. Add it to the original file's header instead. |
| `council-review.py code` exits 4 with "untracked source file(s)" | Commit the untracked files, or re-run with `--allow-untracked` for a pre-commit approach-review. |

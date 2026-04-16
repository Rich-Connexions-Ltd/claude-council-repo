# Claude-Sprint Starter

[![CI](https://github.com/Rich-Connexions-Ltd/claude-council-dev/actions/workflows/ci.yml/badge.svg)](https://github.com/Rich-Connexions-Ltd/claude-council-dev/actions/workflows/ci.yml)

> *Using this template? Replace the badge URL above with your own repo
> slug (`<org>/<repo>`) so the status reflects **your** CI. The
> workflow file at `.github/workflows/ci.yml` ships with every
> bootstrapped project.*

A GitHub template for building software projects with:

- **Council of Experts review** — four parallel LLM reviewers (Claude CLI
  with live MCP access) plus a cross-family consolidator. Catches
  hallucinations, enforces convergence, works under human review or
  fully automated.
- **Sprint process** — structured planning, review, implementation,
  archival. `Sprint N` in Claude Code spins up a new sprint; `Complete`
  wraps one up.
- **Tiered knowledge base** — upload your domain documents and the
  bootstrap wizard summarises them into layered reference files Claude
  reads on demand.
- **Language-aware codegraph** — SQLite index of symbols, imports,
  endpoints, models, tests, and structured file headers across your
  stack. Ships with indexers for Python, TypeScript/JavaScript, Go,
  Rust, Java, plus a generator for any other language with a
  tree-sitter grammar.
- **File-header convention** — every source file carries a structured
  header (Purpose, Role, Exports, Depends on, Invariants, Last updated).
  Lint + PostToolUse hook keep them accurate automatically.

## Getting started

1. Click **Use this template** on GitHub. Clone the new repo locally.
2. Install Python 3.12+. Optional: install the languages you'll use.
3. Run the bootstrap wizard:
   ```bash
   python3 scripts/bootstrap.py
   ```
4. Answer the prompts:
   - Project name and one-line MVP outcome.
   - Stack selection (checkbox list).
   - Upload domain docs into `knowledge/raw/` if you have them.
   - First 3–5 sprints.
   - Review mode: human-in-loop or fully automated.
5. Open the repo in Claude Code. Type `Sprint 1` to start.

## Prerequisites

| Tool | Required | Why |
|---|---|---|
| Python ≥ 3.12 | yes | Bootstrap, indexer, council scripts |
| `claude` CLI (logged in) | yes | Council reviewers |
| `codex` CLI (logged in) | recommended | Consolidator cross-family diversity |
| `GOOGLE_API_KEY` env var | recommended | Gemini fallback |
| Language toolchains | per stack | Rust: `cargo`; Go: `go`; TS: `node`; etc. |

## What's in the box

```
your-new-repo/
├── CLAUDE.md                # Instructions for Claude Code (edited by bootstrap)
├── SPRINTS.md               # Sprint roadmap (seeded by bootstrap)
├── CHANGES.md               # Change log
├── scripts/
│   ├── bootstrap.py         # The wizard
│   ├── check-headers.py     # Lint file-header convention
│   ├── bump-header.py       # PostToolUse hook: auto-update Last updated line
│   ├── index-codebase.py    # Build/query the codegraph SQLite DB
│   ├── header_parser.py     # Shared header extraction
│   ├── council-review.py    # Council of Experts runner
│   ├── council-config.json  # Reviewer roster + lenses
│   ├── archive-plan.sh      # Archive a completed sprint
│   ├── process-test.py      # End-to-end council pipeline test
│   ├── indexers/            # Per-language codegraph extractors
│   │   ├── python.py
│   │   ├── rust.py
│   │   ├── typescript.py
│   │   ├── go.py
│   │   └── java.py
│   └── bootstrap/           # Meta-prompts for Agent-driven generation
│       ├── generate-indexer-prompt.md
│       ├── summarize-knowledge-prompt.md
│       └── domain-expert-prompt.md
├── knowledge/               # Tier-3 deep reference (seeded by bootstrap)
├── memory/                  # Claude Code auto-memory
├── Documentation/archive/   # Archived sprint plans (per-sprint record)
└── .mcp.json                # Codegraph MCP exposure for Claude Code
```

## What if my language isn't listed?

During bootstrap's Step 2, pick "Other..." and enter the language name.
The wizard runs a meta-prompt (see `scripts/bootstrap/generate-indexer-prompt.md`)
that generates a new indexer for you, patches the header parser, and
shows you the result for review. You accept, edit, or reject before it's
committed.

Most languages with a [tree-sitter grammar](https://tree-sitter.github.io/tree-sitter/)
work — which is most languages.

## Customising after bootstrap

- **Toggle review mode**: in Claude Code, say "human review on" or
  "human review off".
- **Edit the council lenses**: `scripts/council-config.json`.
- **Add a new language indexer**: run
  `python3 scripts/bootstrap.py --add-language`.
- **Re-summarise knowledge after uploading more docs**:
  `python3 scripts/bootstrap.py --resummarise-knowledge`.

## Where this came from

This template was extracted from a working production repository that
ran 128 sprints under the Council process. See `TEMPLATE.md` for the
maintainer's view — what was kept, what was stripped, where the tricky
bits are.

## License

MIT. Use it, fork it, contribute back indexers for your language.

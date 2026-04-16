# TEMPLATE.md — For template maintainers

This document is the maintainer-facing view of the Claude-Sprint
starter. If you're a user of a repo created FROM this template, you
want `README.md` instead — close this file.

## What's here and why

This template was extracted from a working production repository
(`VVP`) that ran through 128 sprints under a predecessor of the Council
v6 process. The extraction kept the portable parts and stripped the
domain-specific ones. This document records what was kept, what was
stripped, and where the tricky bits live.

## Extraction decisions

| Kept | Why |
|---|---|
| Council runner + config | Core value prop |
| Codegraph indexer + drift checks | Reviewer quality depends on it |
| Header convention + PostToolUse hook | Keeps codegraph accurate |
| Sprint process (`Sprint N` / `Complete`) | Proven, reasonably portable |
| Per-language indexers (Python, TS/JS, Go, Rust, Java) | Covers ~80% of stacks |
| Meta-prompts under `scripts/bootstrap/` | The "Other..." escape hatch |
| `process-test.py` | End-to-end pipeline validation |

| Stripped | Why |
|---|---|
| `services/` and domain code | VVP-specific |
| `keripy/` vendored lib | VVP-specific |
| `Documentation/` archive | VVP-specific; template ships a blank archive dir |
| `knowledge/` content (kept the directory) | User seeds via bootstrap Step 3 |
| Domain expert lens (KERI/ACDC) | User generates via bootstrap Step 6 |
| PBX deployment scripts | VVP-specific |
| `.e2e-config`, mock vLEI bootstrap | VVP-specific |

## How the bootstrap wizard works

`scripts/bootstrap.py` runs an 8-step interactive flow. The novel bits
are Steps 2, 3, 6 which invoke `claude -p` subprocesses with meta-
prompts under `scripts/bootstrap/`:

- **Step 2 "Other..." language** → `generate-indexer-prompt.md`. Output
  is reviewed and either committed or rejected.
- **Step 3 knowledge seeding** → `summarize-knowledge-prompt.md`.
  Reads `knowledge/raw/`, writes `knowledge/*.md` + updates
  `CLAUDE.md` Tier-3 index.
- **Step 6 domain expert** → `domain-expert-prompt.md`. Reads
  `knowledge/`, writes a lens into `scripts/council-config.json`.

Each meta-prompt demands a JSON summary hand-off so the wizard can
show the user a compact view of what changed.

## Adding a new canned indexer

Steps:

1. Write `scripts/indexers/<lang>.py` — study `rust.py` as the gold
   standard. One parser at module-level, `_walk` function returning
   `{symbols, imports, tests}`.
2. Add comment-style extraction in `scripts/header_parser.py`.
3. Add suffix to `SOURCE_EXTENSIONS` in `scripts/check-headers.py`.
4. Add dispatch in `scripts/index-codebase.py::_index_<lang>_file` and
   the extension branch in `index_all`.
5. Add assertion for the new `comment_style` in
   `_store_header_record`.
6. Add a row to the "Comment syntax per language" table in
   `CLAUDE.md`.
7. Add the indexer as a canned option in `scripts/bootstrap.py`
   (search for `CANNED_INDEXERS`).
8. Test by creating a sample file and running
   `python3 scripts/index-codebase.py`.

The bootstrap's generator prompt handles this automatically for
"Other" languages — but contributing a canned indexer lets future
users skip the generator and get known-good quality.

## Upgrading the template in existing repos

Repos created from the template diverge immediately (bootstrap
customises CLAUDE.md, council-config.json, etc.). We don't maintain a
"pull template updates" flow. If you want to ship an improvement to
existing users:

1. Commit the change here.
2. Document it under `CHANGELOG.md` (this file? we haven't added one
   yet — add when first substantive upgrade ships).
3. Users who want the change manually patch their repo.

## Known rough edges

- The generator prompt (Option-B hybrid generator for new languages)
  assumes `tree-sitter-languages` has a grammar for the target. For
  obscure languages this isn't true. Fallback: skip codegraph indexing
  for that language and run with only header-based navigation.
- `process-test.py` doesn't yet cover the "Other..." language
  generator path. Add to test matrix when generator is hardened.

## TODOs

1. **Test the end-to-end bootstrap flow** on at least three stacks:
   Python/FastAPI, TS/Next.js, Go/chi.
2. **Document the council-retrospective flow** in CLAUDE.md — it
   exists in VVP's "Complete" command but didn't make the template cut.
3. **Add a CI workflow template** under `.github/workflows/` for
   header linting + codegraph build on PRs.
4. **Script a "pull template updates"** flow as a best-effort rebase.

## License

MIT.

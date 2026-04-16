---
name: sprint-complete
description: Wrap up a sprint — update SPRINTS.md, audit headers, close findings, archive the plan. Run on "Complete".
---

# Sprint complete

This skill orchestrates Phase 3 of the sprint process.

## Steps

1. Update `SPRINTS.md` to reflect completed work for Sprint N.
2. Update `knowledge/` for anything that shifted (architecture, models, APIs).
3. Run `./scripts/check-headers.py --sprint <N>` and fix any warnings.
4. Re-read each touched file and update `Last updated` header lines to a one-line summary if the auto-bump placeholder is still present.
5. If `FINDINGS_Sprint<N>.md` exists:
   - Review it for council-process inefficiencies (drip-fed findings, redundant rounds, lens gaps). Update `scripts/council-config.json` if needed.
   - Run `python3 scripts/findings-digest.py` (if the `digest` component is enabled for this profile) and consult `Documentation/FINDINGS_DIGEST.md` when considering council-config tweaks.
6. Ensure every finding has a non-OPEN status (`ADDRESSED`, `WONTFIX`, or `VERIFIED`) with a resolution note.
7. Run `./scripts/archive-plan.sh <N> "<title>"`.
8. Remind the user to update `CHANGES.md` with a sprint summary, then commit and push.

## Gating

- Step 5 sub-item about the digest only applies when `python3 scripts/profile.py is-enabled digest` exits 0.
- On `minimal` profile, this skill still works; council-related steps are no-ops since the council scripts are absent.

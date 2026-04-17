#!/usr/bin/env bash
# File: scripts/archive-plan.sh
# Purpose: Archive an approved sprint plan, append it to PLAN_history.md,
#   preserve FINDINGS to findings-archive/ (profile-gated), and clean up
#   review/round/findings files.
#
# Role:
#   Final step of Phase 3 (Completion and Archival). Appends PLAN_Sprint<N>.md
#   to Documentation/PLAN_history.md, moves it to Documentation/archive/,
#   copies FINDINGS_Sprint<N>.md to Documentation/findings-archive/ when the
#   findings_archive component is enabled, removes transient state files,
#   prints a compaction hint on success, and prompts the user to update
#   CHANGES.md.
#
# Usage:
#   ./scripts/archive-plan.sh <sprint-number> <title>
#
# Example:
#   ./scripts/archive-plan.sh 35 "Credential Issuance"
#
# Last updated: Sprint 6 (2026-04-16) -- terse 3-line default, --verbose restores detail

set -euo pipefail

VERBOSE=0
args=()
for arg in "$@"; do
    if [ "$arg" = "--verbose" ]; then
        VERBOSE=1
    else
        args+=("$arg")
    fi
done
set -- "${args[@]}"

if [ $# -lt 2 ]; then
    echo "Usage: $0 [--verbose] <sprint-number> <title>"
    echo "Example: $0 35 \"Credential Issuance\""
    exit 1
fi

SPRINT_NUM="$1"
shift
TITLE="$*"

if ! [[ "$SPRINT_NUM" =~ ^[0-9]+$ ]]; then
    echo "Error: sprint number must be numeric, got: $SPRINT_NUM" >&2
    exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
PLAN_FILE="$REPO_ROOT/PLAN_Sprint${SPRINT_NUM}.md"
HISTORY_FILE="$REPO_ROOT/Documentation/PLAN_history.md"
ARCHIVE_DIR="$REPO_ROOT/Documentation/archive"
ARCHIVE_FILE="$ARCHIVE_DIR/PLAN_Sprint${SPRINT_NUM}.md"
REVIEW_FILE="$REPO_ROOT/REVIEW_Sprint${SPRINT_NUM}.md"

PLAN_BASENAME="PLAN_Sprint${SPRINT_NUM}.md"
REVIEW_BASENAME="REVIEW_Sprint${SPRINT_NUM}.md"

# --- Validate ---

if [ ! -f "$PLAN_FILE" ]; then
    echo "Error: $PLAN_BASENAME not found at $PLAN_FILE"
    echo "Nothing to archive."
    exit 1
fi

if [ -f "$ARCHIVE_FILE" ]; then
    echo "Error: $ARCHIVE_FILE already exists."
    echo "Sprint $SPRINT_NUM appears to have been archived already."
    exit 1
fi

# --- Ensure directories exist ---

mkdir -p "$ARCHIVE_DIR"

# --- Step 1: Append to PLAN_history.md ---

echo ""
echo "==> Appending to PLAN_history.md..."

{
    echo ""
    echo "---"
    echo ""
    echo "# Sprint ${SPRINT_NUM}: ${TITLE}"
    echo ""
    echo "_Archived: $(date +%Y-%m-%d)_"
    echo ""
    cat "$PLAN_FILE"
    echo ""
} >> "$HISTORY_FILE"

echo "    Done. Plan appended under 'Sprint ${SPRINT_NUM}: ${TITLE}'"

# --- Step 2: Move plan to archive ---

echo ""
echo "==> Moving $PLAN_BASENAME to archive..."
cp "$PLAN_FILE" "$ARCHIVE_FILE"
rm "$PLAN_FILE"
echo "    Done. Archived as $(basename "$ARCHIVE_FILE")"

# --- Step 3: Remove review file ---

echo ""
REVIEW_REMOVED=0
if [ -f "$REVIEW_FILE" ]; then
    echo "==> Removing $REVIEW_BASENAME..."
    rm "$REVIEW_FILE"
    REVIEW_REMOVED=1
    echo "    Done."
else
    echo "==> No $REVIEW_BASENAME to remove (already clean)."
fi

# --- Step 3b: Remove round tracking state files ---

PLAN_ROUND_FILE="$REPO_ROOT/.review-round-sprint${SPRINT_NUM}-plan"
CODE_ROUND_FILE="$REPO_ROOT/.review-round-sprint${SPRINT_NUM}-code"
BASE_COMMIT_FILE="$REPO_ROOT/.sprint-base-commit-${SPRINT_NUM}"
FINDINGS_FILE="$REPO_ROOT/FINDINGS_Sprint${SPRINT_NUM}.md"

# Preserve FINDINGS to durable archive before deletion, if the
# findings_archive component is enabled for this profile.
# profile.py exit codes: 0 = enabled, 1 = not enabled, 2 = error.
if [ -f "$FINDINGS_FILE" ]; then
    set +e
    python3 "$REPO_ROOT/scripts/profile.py" is-enabled findings_archive >/dev/null
    PROFILE_RC=$?
    set -e
    case "$PROFILE_RC" in
        0)
            FINDINGS_ARCHIVE_DIR="$REPO_ROOT/Documentation/findings-archive"
            mkdir -p "$FINDINGS_ARCHIVE_DIR"
            cp "$FINDINGS_FILE" "$FINDINGS_ARCHIVE_DIR/FINDINGS_Sprint${SPRINT_NUM}.md"
            echo "==> Preserved FINDINGS to $FINDINGS_ARCHIVE_DIR/"
            ;;
        1)
            ;;  # component disabled; skip
        *)
            echo "Error: profile.py failed (exit $PROFILE_RC) while checking findings_archive gate." >&2
            echo "Aborting archival to avoid deleting FINDINGS_Sprint${SPRINT_NUM}.md." >&2
            exit 3
            ;;
    esac
fi

for rf in "$PLAN_ROUND_FILE" "$CODE_ROUND_FILE" "$BASE_COMMIT_FILE" "$FINDINGS_FILE"; do
    if [ -f "$rf" ]; then
        echo "==> Removing: $(basename "$rf")"
        rm "$rf"
    fi
done

# --- Step 4: Remind about CHANGES.md ---
#
# Sprint 6: terse 3-line default. --verbose restores the full list.

echo ""
echo "==> Archived Sprint ${SPRINT_NUM}: ${TITLE}"
echo "    Next: update CHANGES.md with summary + commit SHA, then commit docs."

if [ "$VERBOSE" = "1" ]; then
    echo ""
    echo "Files modified:"
    echo "  - Documentation/PLAN_history.md  (appended)"
    echo "  - Documentation/archive/PLAN_Sprint${SPRINT_NUM}.md  (created)"
    echo "  - $PLAN_BASENAME  (removed)"
    [ "$REVIEW_REMOVED" = "1" ] && echo "  - $REVIEW_BASENAME  (removed)"
fi

# Compaction hint (gated by profile component).
if python3 "$REPO_ROOT/scripts/profile.py" is-enabled compaction >/dev/null 2>&1; then
    echo ""
    echo "→ Milestone reached. Consider running /compact before continuing." >&2
fi

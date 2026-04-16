#!/usr/bin/env python3
"""File: scripts/check-headers.py

Purpose: Lint every source file in the repo to confirm it carries a well-formed
file header block and, optionally, that files changed this sprint advertise the
current sprint in their Last updated line.

Role:
  Tooling helper that backs the "File Header Blocks" convention documented in
  CLAUDE.md. Walks the repo, locates source files, scans the first 150 lines
  of each for the required fields (File:, Purpose:, Last updated:), and reports
  missing or malformed headers. Non-strict by default so developers can commit
  work-in-progress without being blocked; use --strict in CI.

Exports:
  - main() -- CLI entry point returning a POSIX exit code
  - check_header(path) -- validate a single file, returning a list of issues
  - check_sprint_staleness(path, sprint) -- verify a changed file's Last updated
  - iter_source_files() -- yield the set of files the linter considers in scope

Depends on:
  - internal: none (stdlib only -- argparse, re, subprocess, pathlib)
  - external: git (for git diff --name-only when --sprint is used)

Invariants & gotchas:
  - Header scan is limited to the first 150 lines so a shebang, license notice,
    or encoding declaration above the header does not hide it.
  - The Last updated regex accepts both em-dash and -- as the separator.
  - Field-name regexes use ^[^A-Za-z0-9]*Field: so the header can sit inside
    Python docstrings, JSDoc blocks, shell comment blocks, etc.
  - Excluded paths are deliberately generous -- if you add a new code directory,
    make sure it is not shadowed by EXCLUDED_DIRS.

Related:
  - CLAUDE.md "File Header Blocks" -- the template and rules this enforces

Last updated: Sprint 5 (2026-04-16) -- --changed-against fallback cascade for missing origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# File extensions that must carry a header block.
SOURCE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".sql",
    ".sh",
    ".yml",
    ".yaml",
    ".rs",
}

# Directories to skip entirely.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    ".next",
}

# Top-level path prefixes to skip (relative to repo root).
EXCLUDED_PREFIXES = (
    ".github/actions/",
)

# Individual files to skip.
EXCLUDED_FILES = {
    "docker-compose.yml",
}

# File-name patterns that are generated / vendored and do not need headers.
EXCLUDED_FILE_PATTERNS = (
    re.compile(r".*\.min\.js$"),
    re.compile(r".*\.d\.ts$"),
)

# Regex: Last updated: Sprint <N> (<YYYY-MM-DD>) -- <message>
# Accepts both em-dash and double hyphen as separators.
# Field regexes allow any non-alphanumeric prefix so the header can sit inside
# Python docstrings, JSDoc blocks, shell/YAML comment blocks, etc.
LAST_UPDATED_RE = re.compile(
    r"Last updated:\s*Sprint\s+(?P<sprint>\d+[a-z]?)\s*"
    r"\((?P<date>\d{4}-\d{2}-\d{2})\)\s*"
    r"(?:---|\u2014|--)\s*(?P<message>.+)"
)

FILE_LINE_RE = re.compile(r"^[^A-Za-z0-9]*File:\s*\S+", re.MULTILINE)
PURPOSE_LINE_RE = re.compile(r"^[^A-Za-z0-9]*Purpose:\s*\S+", re.MULTILINE)

# How many lines at the top of the file we scan for the header block.
HEADER_SCAN_LINES = 150


class HeaderIssue:
    """A single issue found in a file's header."""

    def __init__(self, path: Path, kind: str, detail: str) -> None:
        self.path = path
        self.kind = kind
        self.detail = detail

    def format(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"  {rel}: [{self.kind}] {self.detail}"


def is_excluded(path: Path) -> bool:
    """Check whether a file should be skipped by the linter."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in EXCLUDED_FILES:
        return True
    for prefix in EXCLUDED_PREFIXES:
        if rel.startswith(prefix):
            return True
    for pattern in EXCLUDED_FILE_PATTERNS:
        if pattern.match(rel):
            return True
    parts = set(path.relative_to(REPO_ROOT).parts)
    if parts & EXCLUDED_DIRS:
        return True
    return False


def is_source_file(path: Path) -> bool:
    """Check whether a file is a lintable source file."""
    if path.suffix in SOURCE_EXTENSIONS:
        return True
    if path.name == "Dockerfile" or path.name.startswith("Dockerfile."):
        return True
    return False


def iter_source_files() -> list[Path]:
    """Walk the repo and return all in-scope source files."""
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if not is_source_file(path):
            continue
        if is_excluded(path):
            continue
        files.append(path)
    return sorted(files)


def read_header(path: Path) -> str:
    """Read the first HEADER_SCAN_LINES lines of a file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines: list[str] = []
            for i, line in enumerate(fh):
                if i >= HEADER_SCAN_LINES:
                    break
                lines.append(line)
            return "".join(lines)
    except OSError as e:
        raise RuntimeError(f"could not read {path}: {e}") from e


def check_header(path: Path) -> list[HeaderIssue]:
    """Validate a single file's header, returning a list of issues."""
    issues: list[HeaderIssue] = []
    head = read_header(path)

    if not FILE_LINE_RE.search(head):
        issues.append(
            HeaderIssue(path, "missing-field", "no `File:` line found in header")
        )
    if not PURPOSE_LINE_RE.search(head):
        issues.append(
            HeaderIssue(path, "missing-field", "no `Purpose:` line found in header")
        )

    match = LAST_UPDATED_RE.search(head)
    if not match:
        issues.append(
            HeaderIssue(
                path,
                "missing-field",
                "no `Last updated: Sprint <N> (<YYYY-MM-DD>) -- <message>` line found",
            )
        )
    return issues


def check_sprint_staleness(path: Path, current_sprint: str) -> HeaderIssue | None:
    """Verify a changed file's Last updated line matches the current sprint."""
    head = read_header(path)
    match = LAST_UPDATED_RE.search(head)
    if not match:
        return None  # already reported as missing-field
    if match.group("sprint") != current_sprint:
        return HeaderIssue(
            path,
            "stale-sprint",
            f"changed this sprint but Last updated says Sprint {match.group('sprint')} "
            f"(expected Sprint {current_sprint})",
        )
    return None


DEFAULT_CHANGED_AGAINST = "origin/main"


def _ref_exists(ref: str) -> bool:
    """Return True if ``ref`` resolves to a commit in the local repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _active_base_commit_refs() -> list[tuple[int, str]]:
    """Collect (sprint_num, sha) pairs from any ``.sprint-base-commit-<N>``
    files currently in the repo root. Archive-plan removes these on
    sprint completion, so they reflect *active* sprint state. Returns
    the list sorted by sprint number (highest first)."""
    pairs: list[tuple[int, str]] = []
    for p in REPO_ROOT.glob(".sprint-base-commit-*"):
        suffix = p.name[len(".sprint-base-commit-"):]
        if not suffix.isdigit():
            continue
        try:
            sha = p.read_text().strip()
        except OSError:
            continue
        if not sha:
            continue
        pairs.append((int(suffix), sha))
    pairs.sort(key=lambda t: t[0], reverse=True)
    return pairs


def _iter_fallback_refs():
    """Yield fallback refs in priority order for the
    ``--changed-against`` cascade. Each yield is a human-readable
    label + a ref string the caller can validate with ``_ref_exists``."""
    for sprint, sha in _active_base_commit_refs():
        yield (f"sprint {sprint} base commit", sha)
    yield ("HEAD~1", "HEAD~1")


def _resolve_changed_ref(
    ref: str, *, arg_was_default: bool
) -> str | None:
    """Resolve ``ref`` to something ``git diff`` can use.

    - If ``ref`` already resolves, return it unchanged.
    - If it does not and the caller accepted the default (origin/main),
      walk the fallback cascade and return the first ref that resolves.
    - If it does not and the caller passed an explicit override, return
      ``None`` (caller should signal an error — we don't silently
      swap out a user-specified ref).
    - If nothing in the cascade resolves either, return ``None`` and
      the caller switches to full-scan-with-staleness-suppressed.
    """
    if _ref_exists(ref):
        return ref
    if not arg_was_default:
        return None
    for label, fallback in _iter_fallback_refs():
        if _ref_exists(fallback):
            print(
                f"note: {ref} not found locally; falling back to "
                f"{label} ({fallback}) for staleness diff.",
                file=sys.stderr,
            )
            return fallback
    return None


def git_changed_files(ref: str) -> list[Path]:
    """Get the list of files changed since the given git ref."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{ref}...HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(
            f"warning: could not diff against {ref}: {e.stderr}",
            file=sys.stderr,
        )
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        p = REPO_ROOT / line
        if p.exists() and is_source_file(p) and not is_excluded(p):
            paths.append(p)
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Lint source files for well-formed file header blocks."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any missing/malformed header or stale sprint tag.",
    )
    parser.add_argument(
        "--sprint",
        help="Current sprint number. When set, files changed since "
        "--changed-against must advertise this sprint in Last updated.",
    )
    parser.add_argument(
        "--changed-against",
        default=DEFAULT_CHANGED_AGAINST,
        help="Git ref to diff against for the sprint staleness check "
        f"(default: {DEFAULT_CHANGED_AGAINST}). When the default is "
        "used and the ref is missing (fresh clone, fork), check-headers "
        "cascades through any active .sprint-base-commit-<N> markers "
        "and then HEAD~1 before suppressing the staleness check.",
    )
    return parser


def collect_issues(
    source_files: list[Path],
    sprint: str | None,
    changed_against: str,
    arg_was_default: bool = True,
) -> list[HeaderIssue]:
    """Scan all source files and collect header issues.

    Sprint 5: when a sprint is set, resolve ``changed_against`` through
    the cascade so that a fresh clone or fork without ``origin/main``
    still gets staleness coverage via sprint base commits or ``HEAD~1``.
    If nothing resolves, we skip the staleness check with an explicit
    notice rather than silently reporting no stale files.
    """
    issues: list[HeaderIssue] = []
    for path in source_files:
        issues.extend(check_header(path))
    if sprint:
        resolved = _resolve_changed_ref(
            changed_against, arg_was_default=arg_was_default
        )
        if resolved is None:
            if arg_was_default:
                print(
                    f"note: no usable ref for staleness diff "
                    f"(tried {changed_against}, sprint base commits, "
                    "HEAD~1). Skipping sprint staleness check.",
                    file=sys.stderr,
                )
                return issues
            print(
                f"error: --changed-against {changed_against!r} does not "
                "resolve. Aborting staleness check (refusing to silently "
                "fall back when an explicit ref was provided).",
                file=sys.stderr,
            )
            return issues
        changed = git_changed_files(resolved)
        for path in changed:
            issue = check_sprint_staleness(path, sprint)
            if issue is not None:
                issues.append(issue)
    return issues


def report_issues(
    issues: list[HeaderIssue],
    file_count: int,
    strict: bool,
) -> int:
    """Print issue report and return the exit code."""
    missing = [i for i in issues if i.kind == "missing-field"]
    stale = [i for i in issues if i.kind == "stale-sprint"]

    if missing:
        label = "ERROR" if strict else "WARN"
        print(f"{label}: {len(missing)} file(s) with missing or malformed headers:")
        for issue in missing:
            print(issue.format())

    if stale:
        label = "ERROR" if strict else "WARN"
        print(f"{label}: {len(stale)} file(s) with stale Last updated lines:")
        for issue in stale:
            print(issue.format())

    if not issues:
        print(f"check-headers: OK -- {file_count} source file(s) scanned.")
        return 0

    if strict:
        return 1

    print(
        f"check-headers: scanned {file_count} source file(s), "
        f"{len(issues)} issue(s) reported as warnings."
    )
    return 0


def main() -> int:
    """CLI entry point -- parse args, run checks, report results."""
    parser = build_arg_parser()
    args = parser.parse_args()
    arg_was_default = (
        args.changed_against == parser.get_default("changed_against")
    )
    source_files = iter_source_files()
    issues = collect_issues(
        source_files,
        args.sprint,
        args.changed_against,
        arg_was_default=arg_was_default,
    )
    return report_issues(issues, len(source_files), args.strict)


if __name__ == "__main__":
    sys.exit(main())

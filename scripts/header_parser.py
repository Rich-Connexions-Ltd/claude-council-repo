#!/usr/bin/env python3
"""File: scripts/header_parser.py

Purpose: Parse the structured file header block from the first 150 lines of a
source file into a HeaderRecord dataclass the codegraph indexer can persist
into normalised tables.

Role:
  Pure parsing module -- no I/O, no database, no side effects. Called by
  scripts/index-codebase.py after that module reads a file's content. Knows
  four comment styles (python docstring, jsdoc, # hash-block, -- sql-block)
  and extracts eight fields (File, Purpose, Role, Exports, Depends on,
  Invariants & gotchas, Related, Last updated) plus normalised bullets for
  Exports and Depends on. Tolerates malformed headers: records the failed
  field name in parse_errors and continues.

Exports:
  - HEADER_SCAN_LINES -- module constant: first N lines scanned for header
  - HeaderExport -- dataclass for one Exports bullet (name + description)
  - HeaderDepend -- dataclass for one Depends on bullet (scope, target, reason)
  - HeaderRecord -- dataclass aggregating all extracted fields for one file
  - detect_comment_style -- map filename/extension to "python"|"jsdoc"|"hash"|"sql"
  - parse_header -- main entry point; returns HeaderRecord or None

Depends on:
  - internal: none (stdlib only; no imports of check-headers.py to keep the
    parser independently testable)
  - external: none (re, dataclasses, pathlib from stdlib)

Invariants & gotchas:
  - parse_header MUST NOT raise on malformed input. Field-level failures are
    recorded in HeaderRecord.parse_errors and the corresponding field is None.
  - Comment style detection is deterministic: filename/extension dispatch, no
    content sniffing. Unknown extensions -> parse_header returns None.
  - The parser scans at most HEADER_SCAN_LINES (150) lines. Headers pushed
    past that boundary by shebangs/license blocks are not detected, matching
    check-headers.py behaviour.
  - Multiline fields (Role, Invariants & gotchas, Related) capture from the
    field label until the next recognised field label at column 0 of the
    comment-block content, or until the block ends.
  - Bullets in Exports/Depends on are lines starting with "- " or "* " after
    stripping the comment-style prefix.

Related:
  - scripts/index-codebase.py -- calls parse_header and stores the record
  - scripts/check-headers.py -- parallel regex for presence/freshness checking

Last updated: Sprint 125 (2026-04-13) -- initial parser module
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HEADER_SCAN_LINES = 150

# Ordered list of recognised field labels. Order matters for the multiline
# capture boundary (a Role block ends when any later label appears).
_FIELD_LABELS: tuple[str, ...] = (
    "File",
    "Purpose",
    "Role",
    "Exports",
    "Depends on",
    "Invariants & gotchas",
    "Related",
    "Last updated",
)

_LAST_UPDATED_RE = re.compile(
    r"^Last updated:\s*Sprint\s+(?P<sprint>\d+[a-z]?)\s*"
    r"\((?P<date>\d{4}-\d{2}-\d{2})\)\s*"
    r"(?:---|\u2014|--)\s*(?P<message>.+?)\s*$"
)


@dataclass
class HeaderExport:
    """One bullet from the Exports: list."""

    name: str
    description: str = ""


@dataclass
class HeaderDepend:
    """One bullet from the Depends on: list.

    scope is "internal" or "external".
    target is the module/package name.
    reason is the parenthetical "(for ...)" text, stripped of parens.
    """

    scope: str
    target: str
    reason: str = ""


@dataclass
class HeaderRecord:
    """All extracted fields for one source file's header block."""

    file_path: str
    comment_style: str
    purpose: str | None = None
    role: str | None = None
    invariants: str | None = None
    related: str | None = None
    last_updated_sprint: str | None = None
    last_updated_date: str | None = None
    last_updated_message: str | None = None
    exports: list[HeaderExport] = field(default_factory=list)
    depends: list[HeaderDepend] = field(default_factory=list)
    raw_header: str = ""
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Comment style dispatch
# ---------------------------------------------------------------------------


def detect_comment_style(path: Path) -> str | None:
    """Return comment style for the file, or None if unsupported."""
    name = path.name
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in (".js", ".jsx", ".ts", ".tsx"):
        return "jsdoc"
    if suffix in (".sh", ".yml", ".yaml"):
        return "hash"
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return "hash"
    if suffix == ".sql":
        return "sql"
    if suffix == ".rs":
        return "rust"
    return None


# ---------------------------------------------------------------------------
# Header extraction -- pull out the raw block from a file's head content
# ---------------------------------------------------------------------------


def _extract_block(head: str, comment_style: str) -> str | None:
    """Locate the header block in the file head and return its inner text.

    Returns the content with comment markers stripped line-by-line, so
    downstream parsers see field labels at column 0.
    """
    lines = head.splitlines()
    lines = lines[:HEADER_SCAN_LINES]
    if comment_style == "python":
        return _extract_python_block(lines)
    if comment_style == "jsdoc":
        return _extract_jsdoc_block(lines)
    if comment_style == "hash":
        return _extract_hash_block(lines)
    if comment_style == "sql":
        return _extract_sql_block(lines)
    if comment_style == "rust":
        return _extract_rust_block(lines)
    return None


def _extract_python_block(lines: list[str]) -> str | None:
    """Find the first triple-quoted docstring containing 'File:' and return contents."""
    i = 0
    # Skip shebang / coding / blank lines.
    while i < len(lines) and (
        lines[i].startswith("#!") or lines[i].startswith("# -*-") or lines[i].strip() == ""
    ):
        i += 1
    if i >= len(lines):
        return None
    line = lines[i]
    if '"""' not in line and "'''" not in line:
        return None
    # Capture from opening triple quotes to next triple quotes.
    triple = '"""' if '"""' in line else "'''"
    start = i
    # Remove opening triple from first line.
    first = line.split(triple, 1)[1]
    block_lines: list[str] = []
    if first.strip():
        block_lines.append(first)
    for j in range(start + 1, len(lines)):
        if triple in lines[j]:
            # Add anything before the closing triple.
            before_close = lines[j].split(triple, 1)[0]
            if before_close.strip():
                block_lines.append(before_close)
            break
        block_lines.append(lines[j])
    return "\n".join(block_lines)


def _extract_jsdoc_block(lines: list[str]) -> str | None:
    """Find the first /** ... */ block and strip leading * gutters."""
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or "/**" not in lines[i]:
        return None
    start = i
    block_lines: list[str] = []
    for j in range(start, len(lines)):
        stripped = lines[j]
        # Strip leading whitespace + "/**" or "*/" or " * " gutters.
        stripped = re.sub(r"^\s*/\*\*\s?", "", stripped)
        stripped = re.sub(r"^\s*\*/?\s?", "", stripped)
        if "*/" in lines[j]:
            if stripped.strip():
                block_lines.append(stripped)
            break
        block_lines.append(stripped)
    return "\n".join(block_lines)


def _extract_hash_block(lines: list[str]) -> str | None:
    """Find a contiguous block of # comments, strip the # prefix."""
    i = 0
    # Skip shebang.
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    # Skip blank lines.
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    # Collect contiguous # comment lines.
    if i >= len(lines) or not lines[i].lstrip().startswith("#"):
        return None
    block_lines: list[str] = []
    for j in range(i, len(lines)):
        stripped = lines[j].lstrip()
        if not stripped.startswith("#"):
            break
        # Strip leading # and one optional space.
        content = re.sub(r"^#\s?", "", stripped)
        block_lines.append(content)
    return "\n".join(block_lines)


def _extract_rust_block(lines: list[str]) -> str | None:
    """Find a contiguous block of //! inner doc comments, strip the //! prefix.

    Tolerates leading inner attributes like `#![deny(missing_docs)]` and
    blank lines above the header block (common on crate roots).
    """
    i = 0
    while i < len(lines) and (
        lines[i].strip() == "" or lines[i].lstrip().startswith("#![")
    ):
        i += 1
    if i >= len(lines) or not lines[i].lstrip().startswith("//!"):
        return None
    block_lines: list[str] = []
    for j in range(i, len(lines)):
        stripped = lines[j].lstrip()
        if not stripped.startswith("//!"):
            break
        content = re.sub(r"^//!\s?", "", stripped)
        block_lines.append(content)
    return "\n".join(block_lines)


def _extract_sql_block(lines: list[str]) -> str | None:
    """Find a contiguous block of -- comments, strip the -- prefix."""
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or not lines[i].lstrip().startswith("--"):
        return None
    block_lines: list[str] = []
    for j in range(i, len(lines)):
        stripped = lines[j].lstrip()
        if not stripped.startswith("--"):
            break
        content = re.sub(r"^--\s?", "", stripped)
        block_lines.append(content)
    return "\n".join(block_lines)


# ---------------------------------------------------------------------------
# Field parsing -- split the block into fields
# ---------------------------------------------------------------------------


def _split_fields(block: str) -> dict[str, str]:
    """Split the block text into {field_label: raw_content} dict.

    A field starts at a line matching "^<Label>:" at column 0 and continues
    until the next recognised field label or end of block.
    """
    fields: dict[str, str] = {}
    current_label: str | None = None
    current_lines: list[str] = []
    for line in block.splitlines():
        matched_label: str | None = None
        for label in _FIELD_LABELS:
            # Field label must be at column 0 of the stripped line.
            if line.startswith(label + ":"):
                matched_label = label
                break
        if matched_label is not None:
            if current_label is not None:
                fields[current_label] = "\n".join(current_lines).rstrip()
            current_label = matched_label
            # Keep the rest of the line after the label.
            current_lines = [line[len(matched_label) + 1 :].lstrip()]
        elif current_label is not None:
            current_lines.append(line)
    if current_label is not None:
        fields[current_label] = "\n".join(current_lines).rstrip()
    return fields


def _parse_last_updated(value: str) -> tuple[str | None, str | None, str | None]:
    """Parse a Last updated: line into (sprint, date, message)."""
    # Try the full-match regex against "Sprint N (YYYY-MM-DD) -- message".
    combined = "Last updated: " + value.strip()
    m = _LAST_UPDATED_RE.match(combined)
    if m is None:
        return (None, None, None)
    return (m.group("sprint"), m.group("date"), m.group("message"))


def _parse_exports(value: str) -> list[HeaderExport]:
    """Parse the Exports: block into a list of HeaderExport.

    Skips "Category: item, item" grouping bullets -- those aren't symbol
    names. Accepts only bullets whose Name portion is a single identifier
    (letters, digits, underscore, dot, parentheses for callable hints).
    """
    out: list[HeaderExport] = []
    identifier_re = re.compile(r"^[A-Za-z_][\w.]*(?:\(\))?$")
    for line in value.splitlines():
        bullet = line.lstrip().lstrip("-*").strip()
        if not bullet:
            continue
        # Format: "Name -- description" or "Name — description" or just "Name".
        parts = re.split(r"\s*(?:---|--|\u2014)\s*", bullet, maxsplit=1)
        name = parts[0].strip().rstrip(":")
        description = parts[1].strip() if len(parts) > 1 else ""
        # Normalize trailing parens: "foo()" -> "foo" for symbol matching.
        normalised = name[:-2] if name.endswith("()") else name
        if identifier_re.match(name) or identifier_re.match(normalised):
            out.append(HeaderExport(name=normalised, description=description))
    return out


def _parse_depends(value: str) -> list[HeaderDepend]:
    """Parse the Depends on: block into a list of HeaderDepend."""
    out: list[HeaderDepend] = []
    current_scope: str | None = None
    for line in value.splitlines():
        stripped = line.lstrip()
        # Scope markers: "- internal:" or "- external:" (with or without leading dash).
        scope_match = re.match(
            r"^[-*]?\s*(internal|external):\s*(.*)$", stripped, flags=re.IGNORECASE
        )
        if scope_match:
            current_scope = scope_match.group(1).lower()
            remainder = scope_match.group(2).strip()
            if remainder:
                _append_depend_items(out, current_scope, remainder)
            continue
        # Bullet continuation of the current scope.
        bullet = stripped.lstrip("-*").strip()
        if not bullet or current_scope is None:
            continue
        _append_depend_items(out, current_scope, bullet)
    return out


def _append_depend_items(
    out: list[HeaderDepend], scope: str, text: str
) -> None:
    """Split a comma-separated depend list into individual HeaderDepend rows.

    Skips sentinel values "none" (case-insensitive) that indicate "no deps".
    Skips entries whose target is not a plausible module/package identifier
    (contains braces, spaces-other-than-reason, or is only punctuation).
    """
    # Split on commas NOT inside parentheses.
    parts = re.split(r",(?![^()]*\))", text)
    target_re = re.compile(r"^[A-Za-z_][\w./-]*$")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # "target (for reason)" or just "target".
        m = re.match(r"^(.+?)\s*\((?:for\s+)?(.+?)\)\s*$", part)
        if m:
            target = m.group(1).strip()
            reason = m.group(2).strip()
        else:
            target = part
            reason = ""
        if not target:
            continue
        if target.lower() == "none":
            continue
        # Only accept plausible module/package identifiers.
        if not target_re.match(target):
            continue
        out.append(HeaderDepend(scope=scope, target=target, reason=reason))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_header(path: Path, head_text: str, rel_path: str | None = None) -> HeaderRecord | None:
    """Parse the header of a file. Returns None if the file has no recognised
    comment style or no locatable header block.

    Failures within the header itself (malformed fields, missing bullets) are
    recorded in record.parse_errors rather than raised.
    """
    comment_style = detect_comment_style(path)
    if comment_style is None:
        return None

    try:
        block = _extract_block(head_text, comment_style)
    except Exception as exc:  # pragma: no cover -- defensive
        block = None
        extraction_error = f"extract_block:{type(exc).__name__}"
    else:
        extraction_error = None

    if block is None:
        return None

    record = HeaderRecord(
        file_path=rel_path or str(path),
        comment_style=comment_style,
        raw_header=block,
    )
    if extraction_error is not None:
        record.parse_errors.append(extraction_error)

    try:
        fields = _split_fields(block)
    except Exception as exc:  # pragma: no cover
        record.parse_errors.append(f"split_fields:{type(exc).__name__}")
        return record

    # If the block has none of the required fields, treat as "no header".
    # This filters out incidental docstrings that don't follow the convention.
    if not fields.get("File") and not fields.get("Purpose") and not fields.get("Last updated"):
        return None

    record.purpose = fields.get("Purpose") or None
    record.role = fields.get("Role") or None
    record.invariants = fields.get("Invariants & gotchas") or None
    record.related = fields.get("Related") or None

    last_updated = fields.get("Last updated")
    if last_updated:
        try:
            sprint, date, message = _parse_last_updated(last_updated)
            record.last_updated_sprint = sprint
            record.last_updated_date = date
            record.last_updated_message = message
            if sprint is None:
                record.parse_errors.append("last_updated:malformed")
        except Exception as exc:  # pragma: no cover
            record.parse_errors.append(f"last_updated:{type(exc).__name__}")

    exports_raw = fields.get("Exports")
    if exports_raw:
        try:
            record.exports = _parse_exports(exports_raw)
        except Exception as exc:  # pragma: no cover
            record.parse_errors.append(f"exports:{type(exc).__name__}")

    depends_raw = fields.get("Depends on")
    if depends_raw:
        try:
            record.depends = _parse_depends(depends_raw)
        except Exception as exc:  # pragma: no cover
            record.parse_errors.append(f"depends:{type(exc).__name__}")

    return record

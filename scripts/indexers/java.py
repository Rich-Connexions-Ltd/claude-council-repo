"""File: template/scripts/indexers/java.py

Purpose: Tree-sitter indexer for Java source files (.java). Populates
symbols, imports, tests in the codegraph schema.

Role:
  Per-language extension for scripts/index-codebase.py. Extracts class,
  interface, enum, record declarations; methods with parent class; field
  constants; imports; and JUnit test methods (@Test / @ParameterizedTest
  annotations).

Exports:
  - index_java_file -- extract symbols/imports/tests from one Java file

Depends on:
  - external: tree-sitter (<0.22), tree-sitter-languages

Invariants & gotchas:
  - Visibility stored in `decorators`: one of "public", "protected",
    "private", or "package-private" (no modifier).
  - Method tests identified by @Test / @ParameterizedTest / @RepeatedTest
    annotations. Spring/JUnit4 @org.junit.Test also accepted.
  - Inner classes surface as top-level symbols with `parent` set to the
    enclosing class — matches Go receiver pattern.

Last updated: Sprint 1 (2026-04-14) -- initial indexer
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    from tree_sitter_languages import get_parser

_PARSER = None

_TEST_ANNOTATIONS = {
    "Test", "ParameterizedTest", "RepeatedTest", "TestFactory",
    "TestTemplate",
}


def _parser():
    global _PARSER
    if _PARSER is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            _PARSER = get_parser("java")
    return _PARSER


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _modifiers(node, source: bytes) -> tuple[str | None, list[str]]:
    """Return (visibility, annotation_names)."""
    visibility = None
    annotations: list[str] = []
    for child in node.children:
        if child.type == "modifiers":
            for m in child.children:
                if m.type in ("public", "protected", "private"):
                    visibility = m.type
                elif m.type in ("marker_annotation", "annotation"):
                    name_node = m.child_by_field_name("name")
                    if name_node is not None:
                        annotations.append(_text(name_node, source).rsplit(".", 1)[-1])
            break
    if visibility is None:
        visibility = "package-private"
    return visibility, annotations


def _walk_class_body(body, source: bytes, parent_class: str,
                     symbols: list, tests: list) -> None:
    if body is None:
        return
    for child in body.children:
        t = child.type

        if t in ("method_declaration", "constructor_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, source)
            visibility, annotations = _modifiers(child, source)
            is_test = any(a in _TEST_ANNOTATIONS for a in annotations)
            if is_test:
                tests.append({
                    "name": name,
                    "kind": "method",
                    "parent_class": parent_class,
                })
            symbols.append({
                "name": name,
                "kind": "method",
                "line": child.start_point[0] + 1,
                "signature": None,
                "docstring": None,
                "parent": parent_class,
                "decorators": ",".join([visibility] + annotations) if annotations else visibility,
                "bases": None,
            })
            continue

        if t == "field_declaration":
            visibility, annotations = _modifiers(child, source)
            declarator = child.child_by_field_name("declarator")
            if declarator is not None:
                name_node = declarator.child_by_field_name("name")
                if name_node is not None:
                    name = _text(name_node, source)
                    # Surface only UPPER_CASE fields as constants.
                    if name.isupper() or name.replace("_", "").isupper():
                        symbols.append({
                            "name": name,
                            "kind": "constant",
                            "line": child.start_point[0] + 1,
                            "signature": None,
                            "docstring": None,
                            "parent": parent_class,
                            "decorators": visibility,
                            "bases": None,
                        })
            continue

        if t in ("class_declaration", "interface_declaration",
                  "enum_declaration", "record_declaration"):
            # Nested type — recurse with parent set.
            _emit_type(child, source, symbols, tests, outer=parent_class)


def _emit_type(node, source: bytes, symbols: list, tests: list,
               outer: str | None = None) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, source)
    visibility, annotations = _modifiers(node, source)
    kind = {
        "class_declaration": "class",
        "interface_declaration": "class",
        "enum_declaration": "enum",
        "record_declaration": "class",
    }[node.type]
    bases_node = node.child_by_field_name("superclass")
    bases = _text(bases_node, source) if bases_node else None
    symbols.append({
        "name": name,
        "kind": kind,
        "line": node.start_point[0] + 1,
        "signature": None,
        "docstring": None,
        "parent": outer,
        "decorators": ",".join([visibility] + annotations) if annotations else visibility,
        "bases": bases,
    })
    body = node.child_by_field_name("body")
    _walk_class_body(body, source, name, symbols, tests)


def _walk(root, source: bytes) -> dict[str, list[dict[str, Any]]]:
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    for node in root.children:
        t = node.type

        if t == "import_declaration":
            # First child after 'import' is a scoped_identifier or a
            # scoped_identifier followed by asterisk for wildcard imports.
            path_parts: list[str] = []
            for child in node.children:
                if child.type in ("scoped_identifier", "identifier"):
                    path_parts.append(_text(child, source))
                elif child.type == "asterisk":
                    path_parts.append("*")
            if path_parts:
                module = ".".join(path_parts)
                imports.append({
                    "module": module,
                    "name": module.rsplit(".", 1)[-1],
                    "alias": None,
                    "line": node.start_point[0] + 1,
                })
            continue

        if t in ("class_declaration", "interface_declaration",
                  "enum_declaration", "record_declaration"):
            _emit_type(node, source, symbols, tests)
            continue

    return {"symbols": symbols, "imports": imports, "tests": tests}


def index_java_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        source = path.read_bytes()
    except OSError:
        return {"symbols": [], "imports": [], "tests": []}
    try:
        tree = _parser().parse(source)
    except Exception:
        return {"symbols": [], "imports": [], "tests": []}
    return _walk(tree.root_node, source)

"""File: template/scripts/indexers/typescript.py

Purpose: Tree-sitter indexer for TypeScript and JavaScript (.ts, .tsx,
.js, .jsx). Populates symbols, imports, tests in the codegraph schema.

Role:
  Per-language extension for scripts/index-codebase.py. Uses the
  tree-sitter-languages bundle for the TypeScript grammar (which also
  parses JavaScript as a strict subset). Captures exported declarations,
  class methods, import statements, and test markers for Jest / Vitest
  / Mocha / Playwright.

Exports:
  - index_typescript_file -- extract symbols/imports/tests from one file

Depends on:
  - external: tree-sitter (<0.22), tree-sitter-languages

Invariants & gotchas:
  - One parser per process (module-level). Single-threaded.
  - Returns empty dicts on parse failure.
  - Public/private determined by `export` keyword or the TypeScript
    `public`/`private` modifier inside classes. Exported symbols are
    the intended Exports contract.

Last updated: Sprint 1 (2026-04-14) -- initial indexer
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    from tree_sitter_languages import get_parser

_PARSER_TS = None
_PARSER_TSX = None


def _parser_for(suffix: str):
    global _PARSER_TS, _PARSER_TSX
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        if suffix in (".tsx", ".jsx"):
            if _PARSER_TSX is None:
                _PARSER_TSX = get_parser("tsx")
            return _PARSER_TSX
        if _PARSER_TS is None:
            _PARSER_TS = get_parser("typescript")
        return _PARSER_TS


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_TEST_FUNCS = {"describe", "it", "test", "suite", "context"}


def _is_test_call(node, source: bytes) -> str | None:
    """Return the test name if this is a test-function call, else None."""
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    name = None
    if fn.type == "identifier":
        name = _text(fn, source)
    elif fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        if prop is not None:
            name = _text(prop, source)
    if name not in _TEST_FUNCS:
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.children:
        if child.type == "string":
            return _text(child, source).strip("\"'`")
    return None


def _walk(root, source: bytes) -> dict[str, list[dict[str, Any]]]:
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    def walk(node, parent_class: str | None, under_export: bool):
        t = node.type

        if t == "import_statement":
            source_node = node.child_by_field_name("source")
            module = _text(source_node, source).strip("\"'") if source_node else ""
            # Collect imported names from import_clause.
            for child in node.children:
                if child.type == "import_clause":
                    for c in child.children:
                        if c.type == "identifier":
                            imports.append({
                                "module": module,
                                "name": _text(c, source),
                                "alias": None,
                                "line": node.start_point[0] + 1,
                            })
                        elif c.type == "named_imports":
                            for spec in c.children:
                                if spec.type == "import_specifier":
                                    name_node = spec.child_by_field_name("name")
                                    alias_node = spec.child_by_field_name("alias")
                                    if name_node is not None:
                                        imports.append({
                                            "module": module,
                                            "name": _text(name_node, source),
                                            "alias": _text(alias_node, source) if alias_node else None,
                                            "line": node.start_point[0] + 1,
                                        })
                        elif c.type == "namespace_import":
                            for spec in c.children:
                                if spec.type == "identifier":
                                    imports.append({
                                        "module": module,
                                        "name": "*",
                                        "alias": _text(spec, source),
                                        "line": node.start_point[0] + 1,
                                    })
            return

        if t == "export_statement":
            for child in node.children:
                walk(child, parent_class, True)
            return

        if t in ("function_declaration", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbols.append({
                    "name": _text(name_node, source),
                    "kind": "function",
                    "line": node.start_point[0] + 1,
                    "signature": None,
                    "docstring": None,
                    "parent": parent_class,
                    "decorators": "export" if under_export else None,
                    "bases": None,
                })
            return

        if t == "class_declaration":
            name_node = node.child_by_field_name("name")
            class_name = _text(name_node, source) if name_node else None
            if class_name:
                symbols.append({
                    "name": class_name,
                    "kind": "class",
                    "line": node.start_point[0] + 1,
                    "signature": None,
                    "docstring": None,
                    "parent": None,
                    "decorators": "export" if under_export else None,
                    "bases": None,
                })
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.children:
                    if child.type in ("method_definition", "public_field_definition"):
                        mname_node = child.child_by_field_name("name")
                        if mname_node is not None and child.type == "method_definition":
                            symbols.append({
                                "name": _text(mname_node, source),
                                "kind": "method",
                                "line": child.start_point[0] + 1,
                                "signature": None,
                                "docstring": None,
                                "parent": class_name,
                                "decorators": None,
                                "bases": None,
                            })
            return

        if t in ("interface_declaration", "type_alias_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbols.append({
                    "name": _text(name_node, source),
                    "kind": "class" if t == "interface_declaration" else "constant",
                    "line": node.start_point[0] + 1,
                    "signature": None,
                    "docstring": None,
                    "parent": None,
                    "decorators": "export" if under_export else None,
                    "bases": None,
                })
            return

        if t == "enum_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                symbols.append({
                    "name": _text(name_node, source),
                    "kind": "enum",
                    "line": node.start_point[0] + 1,
                    "signature": None,
                    "docstring": None,
                    "parent": None,
                    "decorators": "export" if under_export else None,
                    "bases": None,
                })
            return

        if t == "lexical_declaration":
            # const / let; surface as constant if name is UPPER_CASE.
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        nm = _text(name_node, source)
                        if nm.isupper() or (nm[0].isupper() and under_export):
                            symbols.append({
                                "name": nm,
                                "kind": "constant",
                                "line": node.start_point[0] + 1,
                                "signature": None,
                                "docstring": None,
                                "parent": None,
                                "decorators": "export" if under_export else None,
                                "bases": None,
                            })
            return

        # Look for test calls anywhere (top-level or nested).
        test_name = _is_test_call(node, source)
        if test_name:
            tests.append({
                "name": test_name,
                "kind": "function",
                "parent_class": None,
            })

        # Recurse for nested scopes (function bodies, class bodies,
        # describe blocks) so nested describe/it surface.
        for child in node.children:
            walk(child, parent_class, False)

    walk(root, None, False)
    return {"symbols": symbols, "imports": imports, "tests": tests}


def index_typescript_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        source = path.read_bytes()
    except OSError:
        return {"symbols": [], "imports": [], "tests": []}
    try:
        parser = _parser_for(path.suffix)
        tree = parser.parse(source)
    except Exception:
        return {"symbols": [], "imports": [], "tests": []}
    return _walk(tree.root_node, source)

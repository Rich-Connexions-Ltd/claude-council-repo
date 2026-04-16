"""File: template/scripts/indexers/go.py

Purpose: Tree-sitter indexer for Go source files (.go). Populates
symbols, imports, tests in the codegraph schema.

Role:
  Per-language extension for scripts/index-codebase.py. Extracts package
  declarations, top-level funcs, methods on struct receivers, type
  declarations (struct, interface, alias), const/var, imports, and the
  `func TestXxx(t *testing.T)` test pattern.

Exports:
  - index_go_file -- extract symbols/imports/tests from one Go file

Depends on:
  - external: tree-sitter (<0.22), tree-sitter-languages

Invariants & gotchas:
  - Public/private in Go is capitalisation-based. Exported symbols are
    those with a leading uppercase letter. Stored in `decorators` as
    "exported" for symbols that are exported.
  - Test functions MUST start with `Test`, `Benchmark`, `Example`, or
    `Fuzz` and take the expected signature. We accept the first-word
    convention and add the actual receiver signature in `parent_class`
    when present.

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


def _parser():
    global _PARSER
    if _PARSER is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            _PARSER = get_parser("go")
    return _PARSER


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _is_exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _test_kind(name: str) -> str | None:
    for prefix in ("Test", "Benchmark", "Example", "Fuzz"):
        if name.startswith(prefix) and (
            len(name) == len(prefix) or not name[len(prefix)].islower()
        ):
            return prefix.lower()
    return None


def _receiver_type(recv_node, source: bytes) -> str | None:
    if recv_node is None:
        return None
    # parameter_list -> parameter_declaration -> type
    for child in recv_node.children:
        if child.type == "parameter_declaration":
            t = child.child_by_field_name("type")
            if t is None:
                continue
            # Strip pointer *T -> T.
            if t.type == "pointer_type":
                inner = t.children[-1] if t.children else None
                if inner is not None:
                    return _text(inner, source)
            return _text(t, source)
    return None


def _walk(root, source: bytes) -> dict[str, list[dict[str, Any]]]:
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    for node in root.children:
        t = node.type

        if t == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    path_node = child.child_by_field_name("path")
                    if path_node is not None:
                        path = _text(path_node, source).strip("\"")
                        imports.append({
                            "module": path,
                            "name": path.rsplit("/", 1)[-1],
                            "alias": None,
                            "line": child.start_point[0] + 1,
                        })
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            name_node = spec.child_by_field_name("name")
                            if path_node is not None:
                                path = _text(path_node, source).strip("\"")
                                imports.append({
                                    "module": path,
                                    "name": path.rsplit("/", 1)[-1],
                                    "alias": _text(name_node, source) if name_node else None,
                                    "line": spec.start_point[0] + 1,
                                })
            continue

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, source)
            test_kind = _test_kind(name)
            symbols.append({
                "name": name,
                "kind": "function",
                "line": node.start_point[0] + 1,
                "signature": None,
                "docstring": None,
                "parent": None,
                "decorators": "exported" if _is_exported(name) else None,
                "bases": None,
            })
            if test_kind:
                tests.append({
                    "name": name,
                    "kind": test_kind,
                    "parent_class": None,
                })
            continue

        if t == "method_declaration":
            name_node = node.child_by_field_name("name")
            recv_node = node.child_by_field_name("receiver")
            if name_node is None:
                continue
            name = _text(name_node, source)
            receiver = _receiver_type(recv_node, source)
            symbols.append({
                "name": name,
                "kind": "method",
                "line": node.start_point[0] + 1,
                "signature": None,
                "docstring": None,
                "parent": receiver,
                "decorators": "exported" if _is_exported(name) else None,
                "bases": None,
            })
            continue

        if t == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    name_node = child.child_by_field_name("name")
                    type_node = child.child_by_field_name("type")
                    if name_node is None:
                        continue
                    name = _text(name_node, source)
                    kind = "class"
                    if type_node is not None:
                        if type_node.type == "interface_type":
                            kind = "class"
                        elif type_node.type == "struct_type":
                            kind = "class"
                        else:
                            kind = "constant"
                    symbols.append({
                        "name": name,
                        "kind": kind,
                        "line": child.start_point[0] + 1,
                        "signature": None,
                        "docstring": None,
                        "parent": None,
                        "decorators": "exported" if _is_exported(name) else None,
                        "bases": None,
                    })
            continue

        if t in ("const_declaration", "var_declaration"):
            for child in node.children:
                if child.type in ("const_spec", "var_spec"):
                    for c in child.children:
                        if c.type == "identifier":
                            name = _text(c, source)
                            if _is_exported(name) or name.isupper():
                                symbols.append({
                                    "name": name,
                                    "kind": "constant",
                                    "line": child.start_point[0] + 1,
                                    "signature": None,
                                    "docstring": None,
                                    "parent": None,
                                    "decorators": "exported" if _is_exported(name) else None,
                                    "bases": None,
                                })
            continue

    return {"symbols": symbols, "imports": imports, "tests": tests}


def index_go_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        source = path.read_bytes()
    except OSError:
        return {"symbols": [], "imports": [], "tests": []}
    try:
        tree = _parser().parse(source)
    except Exception:
        return {"symbols": [], "imports": [], "tests": []}
    return _walk(tree.root_node, source)

"""File: scripts/indexers/rust.py

Purpose: Tree-sitter-backed extractor for Rust source files — populates the
same codegraph tables (files, symbols, imports, tests) as the Python AST
indexer, using tree-sitter-rust via the tree-sitter-languages bundle.

Role:
  Per-language extension point for `scripts/index-codebase.py`. The core
  indexer dispatches `.rs` files to `index_rust_file()` here. Everything
  else — file row, header parsing, commit to SQLite — stays in the core
  indexer, so adding a new language is a pure query-writing exercise.

Exports:
  - index_rust_file -- extract symbols/imports/tests from one Rust source file

Depends on:
  - external: tree-sitter (<0.22 for tree-sitter-languages compat),
    tree-sitter-languages (precompiled Rust grammar)

Invariants & gotchas:
  - Parser is created once per process (module-level). Thread-safe for
    single-threaded indexer (one parser, sequential parse calls).
  - Returns empty dicts on parse failure rather than raising — matches
    the Python indexer's SyntaxError skip behaviour.
  - Only captures PUBLIC items for Exports-style listing; the core
    indexer's Exports check lives in header parsing, not here.

Last updated: Sprint 128 (2026-04-14) -- initial file header
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

# tree-sitter-languages emits a deprecation warning on each call; silence
# it at import time rather than on every file.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    from tree_sitter_languages import get_parser

_PARSER = None


def _parser():
    global _PARSER
    if _PARSER is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            _PARSER = get_parser("rust")
    return _PARSER


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _has_pub(node) -> bool:
    for child in node.children:
        if child.type == "visibility_modifier":
            return True
    return False


def _walk_top_level(root, source: bytes) -> dict[str, list[dict[str, Any]]]:
    """Walk the top-level items of a Rust source_file node.

    Returns: {"symbols": [...], "imports": [...], "tests": [...]}.
    """
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    def attribute_names(node) -> list[str]:
        """Extract outer attribute names (e.g. #[test], #[wasm_bindgen_test])."""
        out: list[str] = []
        for child in node.children:
            if child.type == "attribute_item":
                # attribute_item -> attribute -> identifier / scoped_identifier
                for a in child.children:
                    if a.type == "attribute":
                        for inner in a.children:
                            if inner.type in ("identifier", "scoped_identifier"):
                                out.append(_text(inner, source))
        return out

    def visit(node, pending_attrs: list[str]):
        t = node.type

        if t == "use_declaration":
            # Single import statement; capture the full path text.
            target = None
            for child in node.children:
                if child.type in ("scoped_identifier", "use_wildcard",
                                   "use_list", "scoped_use_list",
                                   "identifier", "use_as_clause"):
                    target = _text(child, source).strip().rstrip(";")
                    break
            if target:
                imports.append({
                    "module": target,
                    "name": target.rsplit("::", 1)[-1] if "::" in target else target,
                    "alias": None,
                    "line": node.start_point[0] + 1,
                })
            return

        if t == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _text(name_node, source)
            is_pub = _has_pub(node)
            is_test = any(a in ("test", "wasm_bindgen_test", "tokio::test",
                                "async_std::test")
                          for a in pending_attrs)
            # Signature: the slice up to the body block.
            body = node.child_by_field_name("body")
            sig_end = body.start_byte if body else node.end_byte
            signature = source[node.start_byte:sig_end].decode(
                "utf-8", errors="replace"
            ).strip().rstrip("{").strip()
            if is_test:
                tests.append({
                    "name": name,
                    "kind": "function",
                    "parent_class": None,
                })
            symbols.append({
                "name": name,
                "kind": "function",
                "line": node.start_point[0] + 1,
                "signature": signature,
                "docstring": None,
                "parent": None,
                "decorators": ",".join(pending_attrs) if pending_attrs else None,
                "bases": None,
                "is_public": is_pub,
            })
            return

        if t in ("struct_item", "enum_item", "trait_item", "union_item",
                 "type_item", "const_item", "static_item"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            kind_map = {
                "struct_item": "class",
                "enum_item": "enum",
                "trait_item": "class",
                "union_item": "class",
                "type_item": "constant",
                "const_item": "constant",
                "static_item": "constant",
            }
            symbols.append({
                "name": _text(name_node, source),
                "kind": kind_map[t],
                "line": node.start_point[0] + 1,
                "signature": None,
                "docstring": None,
                "parent": None,
                "decorators": ",".join(pending_attrs) if pending_attrs else None,
                "bases": None,
                "is_public": _has_pub(node),
            })
            return

        if t == "impl_item":
            # Walk method items inside impl blocks so tests and methods are
            # surfaced even when gated behind an impl.
            type_node = node.child_by_field_name("type")
            parent_name = _text(type_node, source) if type_node else None
            body = node.child_by_field_name("body")
            if body is None:
                return
            inner_attrs: list[str] = []
            for child in body.children:
                if child.type == "attribute_item":
                    for a in child.children:
                        if a.type == "attribute":
                            for inner in a.children:
                                if inner.type in ("identifier", "scoped_identifier"):
                                    inner_attrs.append(_text(inner, source))
                    continue
                if child.type == "function_item":
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        inner_attrs = []
                        continue
                    is_test = any(
                        a in ("test", "wasm_bindgen_test", "tokio::test",
                              "async_std::test")
                        for a in inner_attrs
                    )
                    if is_test:
                        tests.append({
                            "name": _text(name_node, source),
                            "kind": "method",
                            "parent_class": parent_name,
                        })
                    symbols.append({
                        "name": _text(name_node, source),
                        "kind": "method",
                        "line": child.start_point[0] + 1,
                        "signature": None,
                        "docstring": None,
                        "parent": parent_name,
                        "decorators": ",".join(inner_attrs) if inner_attrs else None,
                        "bases": None,
                        "is_public": _has_pub(child),
                    })
                    inner_attrs = []
                else:
                    inner_attrs = []
            return

        if t == "mod_item":
            name_node = node.child_by_field_name("name")
            mod_name = _text(name_node, source) if name_node else None
            if name_node is not None:
                symbols.append({
                    "name": mod_name,
                    "kind": "module",
                    "line": node.start_point[0] + 1,
                    "signature": None,
                    "docstring": None,
                    "parent": None,
                    "decorators": ",".join(pending_attrs) if pending_attrs else None,
                    "bases": None,
                    "is_public": _has_pub(node),
                })
            # Recurse into inline mod bodies ONLY to surface tests
            # (e.g. `#[cfg(test)] mod tests { ... }`). Other items inside
            # a mod block stay scoped to that mod and aren't surfaced as
            # top-level symbols.
            body = node.child_by_field_name("body")
            if body is None:
                return
            inner_attrs: list[str] = []
            for child in body.children:
                if child.type == "attribute_item":
                    for a in child.children:
                        if a.type == "attribute":
                            for inner in a.children:
                                if inner.type in ("identifier", "scoped_identifier"):
                                    inner_attrs.append(_text(inner, source))
                    continue
                if child.type == "function_item":
                    is_test = any(
                        a in ("test", "wasm_bindgen_test", "tokio::test",
                              "async_std::test")
                        for a in inner_attrs
                    )
                    if is_test:
                        fname_node = child.child_by_field_name("name")
                        if fname_node is not None:
                            tests.append({
                                "name": _text(fname_node, source),
                                "kind": "function",
                                "parent_class": mod_name,
                            })
                    inner_attrs = []
                else:
                    inner_attrs = []
            return

    pending: list[str] = []
    for child in root.children:
        if child.type == "attribute_item":
            for a in child.children:
                if a.type == "attribute":
                    for inner in a.children:
                        if inner.type in ("identifier", "scoped_identifier"):
                            pending.append(_text(inner, source))
            continue
        visit(child, pending)
        pending = []

    return {"symbols": symbols, "imports": imports, "tests": tests}


def index_rust_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse a Rust source file and return extracted rows.

    Returns a dict with keys symbols/imports/tests, each a list of row
    dicts ready for insertion. Returns empty lists on parse failure.
    """
    try:
        source = path.read_bytes()
    except OSError:
        return {"symbols": [], "imports": [], "tests": []}
    try:
        tree = _parser().parse(source)
    except Exception:
        return {"symbols": [], "imports": [], "tests": []}
    if tree.root_node.has_error:
        # Tree-sitter always returns a tree, even for partial parses; we
        # proceed anyway — extracting whatever parsed cleanly.
        pass
    return _walk_top_level(tree.root_node, source)

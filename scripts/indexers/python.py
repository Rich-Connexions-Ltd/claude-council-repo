"""File: template/scripts/indexers/python.py

Purpose: Shim exposing the built-in Python AST indexer so the indexer
dispatch layer is uniform across languages.

Role:
  The core `scripts/index-codebase.py` file has the Python pipeline
  baked in (ast-based, framework-aware for FastAPI/Django/etc.). For
  consistency with the per-language indexer pattern used by Rust, Go,
  Java, TypeScript, this shim provides an `index_python_file()` entry
  point. It delegates to the core indexer's internal methods.

  When the codegraph dispatcher sees a .py file it should call into the
  core indexer directly; this module exists so other callers (tests,
  the bootstrap generator's validation step) have a stable import
  surface.

Exports:
  - index_python_file -- extract symbols/imports/tests from a Python file

Depends on:
  - external: ast (stdlib)

Last updated: Sprint 1 (2026-04-14) -- initial shim
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    return f"def {node.name}({', '.join(args)})"


def _decorators(node) -> str | None:
    names: list[str] = []
    for d in getattr(node, "decorator_list", []) or []:
        if isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
        elif isinstance(d, ast.Call):
            fn = d.func
            if isinstance(fn, ast.Name):
                names.append(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.append(fn.attr)
    return ",".join(names) if names else None


def _is_test(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if node.name.startswith("test_"):
        return True
    decs = _decorators(node) or ""
    return "pytest" in decs or "fixture" in decs


def index_python_file(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse a Python file and return symbol/import/test rows."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return {"symbols": [], "imports": [], "tests": []}

    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [
                b.id if isinstance(b, ast.Name) else getattr(b, "attr", "?")
                for b in node.bases
            ]
            symbols.append({
                "name": node.name,
                "kind": "class",
                "line": node.lineno,
                "signature": None,
                "docstring": ast.get_docstring(node),
                "parent": None,
                "decorators": _decorators(node),
                "bases": ",".join(bases) if bases else None,
            })
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({
                        "name": item.name,
                        "kind": "method",
                        "line": item.lineno,
                        "signature": _signature(item),
                        "docstring": ast.get_docstring(item),
                        "parent": node.name,
                        "decorators": _decorators(item),
                        "bases": None,
                    })
                    if _is_test(item):
                        tests.append({
                            "name": item.name,
                            "kind": "method",
                            "parent_class": node.name,
                        })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "kind": "function",
                "line": node.lineno,
                "signature": _signature(node),
                "docstring": ast.get_docstring(node),
                "parent": None,
                "decorators": _decorators(node),
                "bases": None,
            })
            if _is_test(node):
                tests.append({
                    "name": node.name,
                    "kind": "function",
                    "parent_class": None,
                })
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    symbols.append({
                        "name": t.id,
                        "kind": "constant",
                        "line": node.lineno,
                        "signature": None,
                        "docstring": None,
                        "parent": None,
                        "decorators": None,
                        "bases": None,
                    })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "name": alias.name.split(".")[0],
                    "alias": alias.asname,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append({
                    "module": module,
                    "name": alias.name,
                    "alias": alias.asname,
                    "line": node.lineno,
                })

    return {"symbols": symbols, "imports": imports, "tests": tests}

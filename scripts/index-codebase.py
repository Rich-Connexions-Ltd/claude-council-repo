#!/usr/bin/env python3
"""File: scripts/index-codebase.py
Purpose: Parse all VVP Python sources into .claude/codebase.db so Claude Code can query symbols, imports, endpoints, models, and tests before reading files.

Role:
  Tier-0 codegraph builder, invoked manually and via Write/Edit
  PostToolUse hook (incremental mode). Walks SOURCE_DIRS with the ast
  module, populates files/symbols/imports/endpoints/models/tests tables
  plus the Sprint 125 header index (file_headers, file_header_exports,
  file_header_depends). Exposes --stats, --query, --context-for,
  --stale-exports, and --stale-depends for focused retrieval.

Exports:
  - CodebaseIndexer -- main indexer class (index_all, _index_file, etc.)
  - main -- CLI entry point

Depends on:
  - internal: scripts/header_parser.py (parse_header for Sprint 125 index)
  - external: sqlite3, ast (stdlib only)

Last updated: Sprint 6 (2026-04-16) -- --max-lines context-pack cap + render_context_table seam

VVP Codebase Indexer -- builds a SQLite semantic map for Claude Code.

Parses all Python files using the ast module and stores:
  - files: every .py file with metadata
  - symbols: classes, functions, constants with signatures and docstrings
  - imports: the full import graph
  - endpoints: FastAPI routes (method, path, handler)
  - models: Pydantic, dataclass, and SQLAlchemy models with fields
  - tests: test functions/classes mapped to what they test
  - relationships: cross-references (inheritance, calls, dependencies)

Usage:
    python3 scripts/index-codebase.py          # Full rebuild
    python3 scripts/index-codebase.py --stats   # Show DB stats
    python3 scripts/index-codebase.py --query "SELECT * FROM endpoints"
"""

import ast
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# Import the standalone header parser (Sprint 125). Use importlib with an
# explicit module name registered in sys.modules so dataclasses work, without
# polluting sys.path globally.
import importlib.util as _ilu
_hp_spec = _ilu.spec_from_file_location(
    "vvp_header_parser",
    Path(__file__).resolve().parent / "header_parser.py",
)
_hp_mod = _ilu.module_from_spec(_hp_spec)
sys.modules["vvp_header_parser"] = _hp_mod
_hp_spec.loader.exec_module(_hp_mod)
parse_header = _hp_mod.parse_header

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / ".claude" / "codebase.db"

# Expose scripts/ on sys.path so `from indexers.rust import ...` works.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Generic discovery: walk the repo from REPO_ROOT, skipping EXCLUDED_DIR_NAMES
# (matched against any path part) and EXCLUDED_PREFIXES (matched against the
# relative path). No explicit allowlist -- works for any project layout.
EXCLUDED_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", ".next", "target", ".claude",
    "Documentation", "knowledge",
}

EXCLUDED_PREFIXES = (
    ".github/",
)

# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,      -- relative to repo root
    module      TEXT,                       -- dotted module name
    package     TEXT,                       -- top-level package (common, verifier, issuer)
    lines       INTEGER,
    last_modified REAL,
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- class, function, method, constant, enum
    line        INTEGER,
    end_line    INTEGER,
    signature   TEXT,                       -- function/method signature
    docstring   TEXT,
    parent      TEXT,                       -- parent class name (for methods)
    decorators  TEXT,                       -- comma-separated decorator names
    bases       TEXT                        -- comma-separated base classes
);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    imported_module TEXT NOT NULL,          -- the module being imported
    imported_name   TEXT,                   -- specific name (from X import Y)
    alias       TEXT,                       -- import X as alias
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS endpoints (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    method      TEXT NOT NULL,              -- GET, POST, PUT, PATCH, DELETE
    path        TEXT NOT NULL,              -- URL path pattern
    handler     TEXT NOT NULL,              -- function name
    line        INTEGER,
    response_model TEXT,                    -- Pydantic response model name
    dependencies TEXT,                      -- comma-separated Depends() names
    docstring   TEXT,
    router_prefix TEXT                      -- prefix from APIRouter
);

CREATE TABLE IF NOT EXISTS models (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- pydantic, dataclass, sqlalchemy, enum
    line        INTEGER,
    docstring   TEXT,
    table_name  TEXT,                       -- __tablename__ for SQLAlchemy
    bases       TEXT                        -- comma-separated base classes
);

CREATE TABLE IF NOT EXISTS model_fields (
    id          INTEGER PRIMARY KEY,
    model_id    INTEGER REFERENCES models(id),
    name        TEXT NOT NULL,
    type_hint   TEXT,
    [default]   TEXT,
    description TEXT,                       -- from Field(description=...)
    line        INTEGER
);

CREATE TABLE IF NOT EXISTS tests (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- function, method
    line        INTEGER,
    parent_class TEXT,                      -- test class name
    docstring   TEXT
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(imported_module);
CREATE INDEX IF NOT EXISTS idx_imports_name ON imports(imported_name);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_path ON endpoints(path);
CREATE INDEX IF NOT EXISTS idx_endpoints_method ON endpoints(method);
CREATE INDEX IF NOT EXISTS idx_models_name ON models(name);
CREATE INDEX IF NOT EXISTS idx_models_kind ON models(kind);
CREATE INDEX IF NOT EXISTS idx_model_fields_model ON model_fields(model_id);
CREATE INDEX IF NOT EXISTS idx_tests_name ON tests(name);
CREATE INDEX IF NOT EXISTS idx_files_package ON files(package);
CREATE INDEX IF NOT EXISTS idx_files_module ON files(module);

-- Additional indexes for text search
CREATE INDEX IF NOT EXISTS idx_symbols_docstring ON symbols(docstring) WHERE docstring IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);

-- File header index (Sprint 125)
CREATE TABLE IF NOT EXISTS file_headers (
    id                    INTEGER PRIMARY KEY,
    file_id               INTEGER UNIQUE REFERENCES files(id) ON DELETE CASCADE,
    purpose               TEXT,
    role                  TEXT,
    invariants            TEXT,
    related               TEXT,
    last_updated_sprint   TEXT,
    last_updated_date     TEXT,
    last_updated_message  TEXT,
    comment_style         TEXT,
    raw_header            TEXT,
    parse_errors          TEXT,
    parsed_at             REAL
);

CREATE TABLE IF NOT EXISTS file_header_exports (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER REFERENCES files(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS file_header_depends (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER REFERENCES files(id) ON DELETE CASCADE,
    scope           TEXT NOT NULL,
    target          TEXT NOT NULL,
    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_headers_sprint ON file_headers(last_updated_sprint);
CREATE INDEX IF NOT EXISTS idx_file_headers_file ON file_headers(file_id);
CREATE INDEX IF NOT EXISTS idx_file_header_exports_name ON file_header_exports(name);
CREATE INDEX IF NOT EXISTS idx_file_header_exports_file ON file_header_exports(file_id);
CREATE INDEX IF NOT EXISTS idx_file_header_depends_target ON file_header_depends(target);
CREATE INDEX IF NOT EXISTS idx_file_header_depends_file ON file_header_depends(file_id);
"""


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def unparse_safe(node) -> str:
    """Safely unparse an AST node to string."""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def get_docstring(node) -> Optional[str]:
    """Extract docstring from a class or function node."""
    try:
        return ast.get_docstring(node)
    except Exception:
        return None


def get_decorator_names(node) -> list[str]:
    """Extract decorator names from a node."""
    names = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(unparse_safe(dec))
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Attribute):
                names.append(unparse_safe(dec.func))
            elif isinstance(dec.func, ast.Name):
                names.append(dec.func.id)
            else:
                names.append(unparse_safe(dec.func))
    return names


def get_bases(node: ast.ClassDef) -> list[str]:
    """Extract base class names."""
    return [unparse_safe(b) for b in node.bases]


def get_function_signature(node: ast.FunctionDef) -> str:
    """Build a readable function signature."""
    args = node.args
    parts = []

    # Regular args
    all_args = args.args
    defaults = args.defaults
    n_defaults = len(defaults)
    n_args = len(all_args)

    for i, arg in enumerate(all_args):
        name = arg.arg
        if name == "self" or name == "cls":
            continue
        annotation = f": {unparse_safe(arg.annotation)}" if arg.annotation else ""
        # Check if this arg has a default
        default_idx = i - (n_args - n_defaults)
        if default_idx >= 0:
            default = f" = {unparse_safe(defaults[default_idx])}"
        else:
            default = ""
        parts.append(f"{name}{annotation}{default}")

    # *args
    if args.vararg:
        ann = f": {unparse_safe(args.vararg.annotation)}" if args.vararg.annotation else ""
        parts.append(f"*{args.vararg.arg}{ann}")

    # **kwargs
    if args.kwarg:
        ann = f": {unparse_safe(args.kwarg.annotation)}" if args.kwarg.annotation else ""
        parts.append(f"**{args.kwarg.arg}{ann}")

    sig = f"({', '.join(parts)})"

    # Return type
    if node.returns:
        sig += f" -> {unparse_safe(node.returns)}"

    return sig


def extract_field_info(value_node) -> dict:
    """Extract info from Field(...) or Column(...) calls."""
    info = {}
    if not isinstance(value_node, ast.Call):
        # Simple default value
        info["default"] = unparse_safe(value_node)
        return info

    func_name = ""
    if isinstance(value_node.func, ast.Name):
        func_name = value_node.func.id
    elif isinstance(value_node.func, ast.Attribute):
        func_name = value_node.func.attr

    if func_name in ("Field", "Column", "field"):
        for kw in value_node.keywords:
            if kw.arg == "description":
                if isinstance(kw.value, (ast.Constant,)):
                    info["description"] = kw.value.value
            elif kw.arg == "default":
                info["default"] = unparse_safe(kw.value)
            elif kw.arg == "default_factory":
                info["default"] = f"factory({unparse_safe(kw.value)})"
        # Positional args
        if value_node.args:
            info["default"] = unparse_safe(value_node.args[0])

    return info


def detect_router_prefix(tree: ast.Module) -> Optional[str]:
    """Find APIRouter(prefix=...) in module-level assignments."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Call):
                func = node.value.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name == "APIRouter":
                    for kw in node.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            return kw.value.value
    return None


def is_endpoint_decorator(dec) -> Optional[tuple[str, dict]]:
    """Check if a decorator is a FastAPI endpoint. Returns (method, kwargs) or None."""
    if not isinstance(dec, ast.Call):
        return None

    func = dec.func
    attr_name = ""
    if isinstance(func, ast.Attribute):
        attr_name = func.attr
    else:
        return None

    http_methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    if attr_name not in http_methods:
        return None

    kwargs = {}
    # First positional arg is the path
    if dec.args:
        if isinstance(dec.args[0], ast.Constant):
            kwargs["path"] = dec.args[0].value

    for kw in dec.keywords:
        if kw.arg == "response_model":
            kwargs["response_model"] = unparse_safe(kw.value)

    return attr_name.upper(), kwargs


def extract_depends(node: ast.FunctionDef) -> list[str]:
    """Extract Depends() dependency names from function parameters."""
    deps = []
    for default in node.args.defaults + node.args.kw_defaults:
        if default is None:
            continue
        if isinstance(default, ast.Call):
            func = default.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "Depends" and default.args:
                dep_name = unparse_safe(default.args[0])
                deps.append(dep_name)
    return deps


# ---------------------------------------------------------------------------
# Path → module mapping
# ---------------------------------------------------------------------------

def path_to_module(rel_path: str) -> str:
    """Convert a file path to a dotted module name."""
    p = rel_path.replace("/", ".").replace("\\", ".")
    if p.endswith(".py"):
        p = p[:-3]
    if p.endswith(".__init__"):
        p = p[:-9]
    return p


def path_to_package(rel_path: str) -> str:
    """Determine top-level package from path.

    Generic: uses the first path segment as the package name. For files
    directly at the repo root, returns "root".
    """
    parts = Path(rel_path).parts
    if len(parts) <= 1:
        return "root"
    return parts[0]


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------

class CodebaseIndexer:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(str(db_path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.stats = {
            "files": 0, "symbols": 0, "imports": 0,
            "endpoints": 0, "models": 0, "fields": 0, "tests": 0,
        }

    def clear(self):
        """Drop all data for a fresh rebuild."""
        for table in ["model_fields", "tests", "endpoints",
                       "models", "imports", "symbols",
                       "file_header_exports", "file_header_depends", "file_headers",
                       "files"]:
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    def index_all(self, incremental: bool = False):
        """Index all Python files in SOURCE_DIRS.

        If incremental=True, only re-index files whose mtime has changed.
        """
        # Build map of existing file mtimes for incremental mode
        existing = {}
        if incremental:
            for row in self.db.execute("SELECT path, last_modified FROM files"):
                existing[row[0]] = row[1]

        if not incremental:
            self.clear()

        source_files = []
        for pattern in ("*.py", "*.rs"):
            for src_file in REPO_ROOT.rglob(pattern):
                rel_parts = src_file.relative_to(REPO_ROOT).parts
                if any(p in EXCLUDED_DIR_NAMES or p.startswith(".") for p in rel_parts[:-1]):
                    continue
                rel_path = "/".join(rel_parts)
                if any(rel_path.startswith(pfx) for pfx in EXCLUDED_PREFIXES):
                    continue
                source_files.append(src_file)

        current_paths = set()
        changed = 0
        for src_file in sorted(source_files):
            rel_path = str(src_file.relative_to(REPO_ROOT))
            current_paths.add(rel_path)

            if incremental:
                mtime = src_file.stat().st_mtime
                if rel_path in existing and existing[rel_path] == mtime:
                    continue  # Unchanged
                # Remove old data for this file before re-indexing
                self._remove_file(rel_path)
                changed += 1

            if src_file.suffix == ".py":
                self._index_file(src_file)
            elif src_file.suffix == ".rs":
                self._index_rust_file(src_file)

        # Remove deleted files
        if incremental:
            for old_path in existing:
                if old_path not in current_paths:
                    self._remove_file(old_path)
                    changed += 1

        self.db.commit()

        if incremental and changed == 0:
            return None  # Nothing changed — silent exit

        return self.stats

    def _remove_file(self, rel_path: str):
        """Remove all data for a file."""
        row = self.db.execute("SELECT id FROM files WHERE path = ?", (rel_path,)).fetchone()
        if not row:
            return
        file_id = row[0]
        # Remove model fields via model ids
        self.db.execute(
            "DELETE FROM model_fields WHERE model_id IN "
            "(SELECT id FROM models WHERE file_id = ?)", (file_id,)
        )
        for table in ["tests", "endpoints", "models", "imports", "symbols",
                      "file_header_exports", "file_header_depends", "file_headers"]:
            self.db.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
        self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def _index_rust_file(self, filepath: Path):
        """Parse and index a single Rust file via tree-sitter.

        Mirrors _index_file's shape: file row, symbols/imports/tests, then
        header. Gracefully no-ops if tree-sitter-languages is unavailable.
        """
        rel_path = str(filepath.relative_to(REPO_ROOT))
        try:
            from indexers.rust import index_rust_file
        except ImportError:
            return  # Optional dependency; skip silently.

        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        line_count = source.count("\n") + 1
        mtime = filepath.stat().st_mtime

        # Derive a module string from the file path (Rust crate-relative).
        mod_parts = rel_path[:-3].split("/")  # drop .rs
        module = "::".join(mod_parts[-3:])
        package = mod_parts[-2] if len(mod_parts) >= 2 else ""

        self.db.execute(
            "INSERT INTO files (path, module, package, lines, last_modified, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rel_path, module, package, line_count, mtime, time.time())
        )
        file_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.stats["files"] += 1

        extracted = index_rust_file(filepath)
        for sym in extracted["symbols"]:
            self.db.execute(
                "INSERT INTO symbols (file_id, name, kind, line, signature, "
                "docstring, parent, decorators, bases) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, sym["name"], sym["kind"], sym["line"],
                 sym.get("signature"), sym.get("docstring"),
                 sym.get("parent"), sym.get("decorators"),
                 sym.get("bases"))
            )
            self.stats["symbols"] += 1
        for imp in extracted["imports"]:
            self.db.execute(
                "INSERT INTO imports (file_id, imported_module, imported_name, alias, line) "
                "VALUES (?, ?, ?, ?, ?)",
                (file_id, imp["module"], imp["name"], imp.get("alias"), imp["line"])
            )
            self.stats["imports"] += 1
        for t in extracted["tests"]:
            self.db.execute(
                "INSERT INTO tests (file_id, name, kind, parent_class) "
                "VALUES (?, ?, ?, ?)",
                (file_id, t["name"], t["kind"], t.get("parent_class"))
            )
            self.stats["tests"] += 1

        head_text = "\n".join(source.splitlines()[:150])
        record = parse_header(filepath, head_text, rel_path=rel_path)
        if record is not None:
            self._store_header_record(file_id, record)

    def _index_file(self, filepath: Path):
        """Parse and index a single Python file."""
        rel_path = str(filepath.relative_to(REPO_ROOT))

        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            return  # Skip unparseable files

        line_count = source.count("\n") + 1
        mtime = filepath.stat().st_mtime
        module = path_to_module(rel_path)
        package = path_to_package(rel_path)

        # Insert file record
        self.db.execute(
            "INSERT INTO files (path, module, package, lines, last_modified, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rel_path, module, package, line_count, mtime, time.time())
        )
        file_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.stats["files"] += 1

        # Detect router prefix for this module
        router_prefix = detect_router_prefix(tree)

        # Walk the AST
        self._index_imports(file_id, tree)
        self._index_top_level(file_id, tree, rel_path, router_prefix)

        # Parse and store header (Sprint 125). Uses the already-loaded source
        # so no second filesystem read happens.
        head_text = "\n".join(source.splitlines()[:150])
        record = parse_header(filepath, head_text, rel_path=rel_path)
        if record is not None:
            self._store_header_record(file_id, record)

    def _store_header_record(self, file_id: int, record) -> None:
        """Persist a HeaderRecord into file_headers and its child tables.

        Input validation per Holzmann H5; defensive assertions catch caller bugs.
        """
        assert isinstance(file_id, int) and file_id > 0, \
            f"file_id must be a positive int, got {file_id!r}"
        assert record is not None, "record must not be None"
        assert record.comment_style in {"python", "jsdoc", "hash", "sql", "rust"}, \
            f"unknown comment_style: {record.comment_style!r}"

        # Store parse_errors as JSON so downstream queries can parse them
        # reliably (vs. splitting a CSV that might contain commas in error msgs).
        parse_errors = json.dumps(record.parse_errors) if record.parse_errors else None
        self.db.execute(
            "INSERT INTO file_headers (file_id, purpose, role, invariants, related, "
            "last_updated_sprint, last_updated_date, last_updated_message, "
            "comment_style, raw_header, parse_errors, parsed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                record.purpose,
                record.role,
                record.invariants,
                record.related,
                record.last_updated_sprint,
                record.last_updated_date,
                record.last_updated_message,
                record.comment_style,
                record.raw_header,
                parse_errors,
                time.time(),
            ),
        )
        for exp in record.exports:
            self.db.execute(
                "INSERT INTO file_header_exports (file_id, name, description) VALUES (?, ?, ?)",
                (file_id, exp.name, exp.description or None),
            )
        for dep in record.depends:
            self.db.execute(
                "INSERT INTO file_header_depends (file_id, scope, target, reason) "
                "VALUES (?, ?, ?, ?)",
                (file_id, dep.scope, dep.target, dep.reason or None),
            )

    def _index_imports(self, file_id: int, tree: ast.Module):
        """Index all import statements."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.db.execute(
                        "INSERT INTO imports (file_id, imported_module, imported_name, alias, line) "
                        "VALUES (?, ?, NULL, ?, ?)",
                        (file_id, alias.name, alias.asname, node.lineno)
                    )
                    self.stats["imports"] += 1
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in (node.names or []):
                    self.db.execute(
                        "INSERT INTO imports (file_id, imported_module, imported_name, alias, line) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (mod, alias.name, alias.asname, node.lineno) if False else
                        (file_id, mod, alias.name, alias.asname, node.lineno)
                    )
                    self.stats["imports"] += 1

    def _index_top_level(self, file_id: int, tree: ast.Module, rel_path: str, router_prefix: Optional[str]):
        """Index classes, functions, and constants at module level."""
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                self._index_class(file_id, node, rel_path, router_prefix)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._index_function(file_id, node, rel_path, router_prefix, parent=None)
            elif isinstance(node, ast.Assign):
                self._index_constant(file_id, node)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                # Annotated module-level assignment (e.g. FOO: list[str] = [...]).
                self._index_annotated_constant(file_id, node)

    def _index_class(self, file_id: int, node: ast.ClassDef, rel_path: str, router_prefix: Optional[str]):
        """Index a class definition, its methods, and model fields."""
        decorators = get_decorator_names(node)
        bases = get_bases(node)
        docstring = get_docstring(node)

        # Determine what kind of class this is
        kind = "class"
        model_kind = None

        base_names = [b.split(".")[-1] for b in bases]
        if "BaseModel" in base_names:
            model_kind = "pydantic"
            kind = "class"
        elif "DeclarativeBase" in base_names or "Base" in base_names:
            model_kind = "sqlalchemy"
        elif "dataclass" in decorators:
            model_kind = "dataclass"
        elif any(b in ("str, Enum", "Enum", "IntEnum") or "Enum" in b for b in bases):
            model_kind = "enum"
            kind = "enum"

        # Insert symbol
        self.db.execute(
            "INSERT INTO symbols (file_id, name, kind, line, end_line, signature, docstring, parent, decorators, bases) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (file_id, node.name, kind, node.lineno, node.end_lineno,
             docstring, ",".join(decorators), ",".join(bases))
        )
        self.stats["symbols"] += 1

        # Index as a model if applicable
        model_id = None
        if model_kind:
            table_name = None
            # Check for __tablename__
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id == "__tablename__":
                            if isinstance(item.value, ast.Constant):
                                table_name = item.value.value

            self.db.execute(
                "INSERT INTO models (file_id, name, kind, line, docstring, table_name, bases) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, node.name, model_kind, node.lineno, docstring,
                 table_name, ",".join(bases))
            )
            model_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.stats["models"] += 1

            # Index fields
            self._index_model_fields(model_id, node, model_kind)

        # Index methods
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._index_function(file_id, item, rel_path, router_prefix, parent=node.name)

        # Index test class
        if node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("test_"):
                        self.db.execute(
                            "INSERT INTO tests (file_id, name, kind, line, parent_class, docstring) "
                            "VALUES (?, ?, 'method', ?, ?, ?)",
                            (file_id, item.name, item.lineno, node.name, get_docstring(item))
                        )
                        self.stats["tests"] += 1

    def _index_model_fields(self, model_id: int, node: ast.ClassDef, model_kind: str):
        """Index fields of a model class."""
        for item in node.body:
            # Annotated assignments: name: Type = Value
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                name = item.target.id
                if name.startswith("_"):
                    continue
                type_hint = unparse_safe(item.annotation) if item.annotation else None
                field_info = extract_field_info(item.value) if item.value else {}

                self.db.execute(
                    "INSERT INTO model_fields (model_id, name, type_hint, [default], description, line) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (model_id, name, type_hint, field_info.get("default"),
                     field_info.get("description"), item.lineno)
                )
                self.stats["fields"] += 1

            # Simple assignments: name = Column(...) or name = Field(...)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        if isinstance(item.value, ast.Call):
                            func_name = ""
                            if isinstance(item.value.func, ast.Name):
                                func_name = item.value.func.id
                            elif isinstance(item.value.func, ast.Attribute):
                                func_name = item.value.func.attr
                            if func_name in ("Column", "relationship", "Field"):
                                field_info = extract_field_info(item.value)
                                # For Column(), extract type from first arg
                                type_hint = None
                                if func_name == "Column" and item.value.args:
                                    type_hint = unparse_safe(item.value.args[0])
                                elif func_name == "relationship" and item.value.args:
                                    type_hint = f"rel({unparse_safe(item.value.args[0])})"

                                self.db.execute(
                                    "INSERT INTO model_fields (model_id, name, type_hint, [default], description, line) "
                                    "VALUES (?, ?, ?, ?, ?, ?)",
                                    (model_id, target.id, type_hint, field_info.get("default"),
                                     field_info.get("description"), item.lineno)
                                )
                                self.stats["fields"] += 1

            # Enum members: NAME = "value"
            if model_kind == "enum" and isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        val = unparse_safe(item.value) if item.value else None
                        self.db.execute(
                            "INSERT INTO model_fields (model_id, name, type_hint, [default], description, line) "
                            "VALUES (?, ?, 'member', ?, NULL, ?)",
                            (model_id, target.id, val, item.lineno)
                        )
                        self.stats["fields"] += 1

    def _index_function(self, file_id: int, node, rel_path: str,
                        router_prefix: Optional[str], parent: Optional[str]):
        """Index a function/method definition, including endpoint detection."""
        decorators = get_decorator_names(node)
        signature = get_function_signature(node)
        docstring = get_docstring(node)
        kind = "method" if parent else "function"

        # Insert symbol
        self.db.execute(
            "INSERT INTO symbols (file_id, name, kind, line, end_line, signature, docstring, parent, decorators, bases) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (file_id, node.name, kind, node.lineno, node.end_lineno,
             signature, docstring, parent, ",".join(decorators))
        )
        self.stats["symbols"] += 1

        # Check for endpoint decorators
        for dec in node.decorator_list:
            result = is_endpoint_decorator(dec)
            if result:
                method, kwargs = result
                path = kwargs.get("path", "")
                full_path = (router_prefix or "") + path
                deps = extract_depends(node)

                self.db.execute(
                    "INSERT INTO endpoints (file_id, method, path, handler, line, "
                    "response_model, dependencies, docstring, router_prefix) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (file_id, method, full_path, node.name, node.lineno,
                     kwargs.get("response_model"), ",".join(deps),
                     docstring, router_prefix)
                )
                self.stats["endpoints"] += 1

        # Check for standalone test functions
        if node.name.startswith("test_") and not parent:
            self.db.execute(
                "INSERT INTO tests (file_id, name, kind, line, parent_class, docstring) "
                "VALUES (?, ?, 'function', ?, NULL, ?)",
                (file_id, node.name, node.lineno, docstring)
            )
            self.stats["tests"] += 1

    def _index_constant(self, file_id: int, node: ast.Assign):
        """Index module-level constants and public bindings.

        Captures UPPER_CASE constants plus lowercase public bindings (not
        starting with _) so Exports: lists that name them can be cross-
        referenced by the drift check. Skips private names starting with _.
        """
        for target in node.targets:
            if isinstance(target, ast.Name) and not target.id.startswith("_"):
                value_repr = unparse_safe(node.value)
                if len(value_repr) > 200:
                    value_repr = value_repr[:200] + "..."
                self.db.execute(
                    "INSERT INTO symbols (file_id, name, kind, line, end_line, signature, docstring, parent, decorators, bases) "
                    "VALUES (?, ?, 'constant', ?, ?, ?, NULL, NULL, NULL, NULL)",
                    (file_id, target.id, node.lineno, node.end_lineno, value_repr)
                )
                self.stats["symbols"] += 1

    def _index_annotated_constant(self, file_id: int, node: ast.AnnAssign):
        """Index an annotated module-level assignment (FOO: type = value)."""
        assert isinstance(node.target, ast.Name)
        name = node.target.id
        if name.startswith("_"):
            return
        value_repr = unparse_safe(node.value) if node.value is not None else ""
        if len(value_repr) > 200:
            value_repr = value_repr[:200] + "..."
        self.db.execute(
            "INSERT INTO symbols (file_id, name, kind, line, end_line, signature, docstring, parent, decorators, bases) "
            "VALUES (?, ?, 'constant', ?, ?, ?, NULL, NULL, NULL, NULL)",
            (file_id, name, node.lineno, node.end_lineno, value_repr)
        )
        self.stats["symbols"] += 1

    def print_stats(self):
        """Print database statistics."""
        print("\n📊 Codebase Index Statistics")
        print("=" * 50)

        for table, label in [
            ("files", "Files indexed"),
            ("symbols", "Symbols (classes, functions, constants)"),
            ("imports", "Import relationships"),
            ("endpoints", "API endpoints"),
            ("models", "Data models"),
            ("model_fields", "Model fields"),
            ("tests", "Test cases"),
        ]:
            count = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {label:.<40} {count:>6}")

        # Package breakdown
        print("\n  By package:")
        for row in self.db.execute(
            "SELECT package, COUNT(*) FROM files GROUP BY package ORDER BY COUNT(*) DESC"
        ):
            print(f"    {row[0]:.<38} {row[1]:>6} files")

        # Model breakdown
        print("\n  Models by kind:")
        for row in self.db.execute(
            "SELECT kind, COUNT(*) FROM models GROUP BY kind ORDER BY COUNT(*) DESC"
        ):
            print(f"    {row[0]:.<38} {row[1]:>6}")

        # Endpoint breakdown
        print("\n  Endpoints by method:")
        for row in self.db.execute(
            "SELECT method, COUNT(*) FROM endpoints GROUP BY method ORDER BY COUNT(*) DESC"
        ):
            print(f"    {row[0]:.<38} {row[1]:>6}")

        db_size = DB_PATH.stat().st_size / 1024
        print(f"\n  Database size: {db_size:.0f} KB")
        print(f"  Location: {DB_PATH}")

    def run_query(self, sql: str):
        """Execute an arbitrary SQL query and print results."""
        try:
            cursor = self.db.execute(sql)
            rows = cursor.fetchall()
            if not rows:
                print("(no results)")
                return
            # Print column headers
            cols = [desc[0] for desc in cursor.description]
            widths = [max(len(str(c)), max(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
            # Cap widths
            widths = [min(w, 60) for w in widths]

            header = " | ".join(c.ljust(w)[:w] for c, w in zip(cols, widths))
            print(header)
            print("-" * len(header))
            for row in rows:
                line = " | ".join(str(v or "").ljust(w)[:w] for v, w in zip(row, widths))
                print(line)
            print(f"\n({len(rows)} rows)")
        except sqlite3.Error as e:
            print(f"SQL error: {e}")

    def generate_context(self, file_paths: list[str]) -> str:
        """Generate a focused codebase context summary for the given files.

        This produces a compact markdown section suitable for embedding in
        review prompts, covering:
        1. High-level codebase stats
        2. Symbols defined in the target files
        3. Import dependencies (what the files use)
        4. Reverse dependencies (what uses the files)
        5. Endpoints defined in/near the target files
        6. Models defined in/near the target files
        7. Related test coverage
        """
        lines = []
        lines.append("## Codebase Structure Context")
        lines.append("")

        # --- 1. High-level stats ---
        total_files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_endpoints = self.db.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
        total_models = self.db.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        total_tests = self.db.execute("SELECT COUNT(*) FROM tests").fetchone()[0]

        lines.append(f"**Codebase:** {total_files} files, {total_endpoints} endpoints, "
                      f"{total_models} models, {total_tests} tests")
        lines.append("")

        # Resolve file IDs and modules
        file_ids = []
        file_modules = []
        for fp in file_paths:
            row = self.db.execute("SELECT id, module FROM files WHERE path = ?", (fp,)).fetchone()
            if row:
                file_ids.append(row[0])
                file_modules.append(row[1])

        if not file_ids:
            lines.append("*No indexed files matched the provided paths.*")
            return "\n".join(lines)

        placeholders = ",".join("?" * len(file_ids))

        # --- 2. Symbols in target files ---
        lines.append("### Symbols in Changed Files")
        lines.append("")
        lines.append("| File | Symbol | Kind | Line | Signature |")
        lines.append("|------|--------|------|------|-----------|")

        rows = self.db.execute(
            f"SELECT f.path, s.name, s.kind, s.line, s.signature "
            f"FROM symbols s JOIN files f ON s.file_id = f.id "
            f"WHERE s.file_id IN ({placeholders}) "
            f"AND s.kind IN ('class', 'function', 'enum', 'constant') "
            f"ORDER BY f.path, s.line",
            file_ids
        ).fetchall()

        for path, name, kind, line, sig in rows:
            short_path = path.split("/")[-1]
            sig_col = f"`{sig}`" if sig and len(sig) < 60 else (sig[:57] + "..." if sig else "")
            lines.append(f"| {short_path} | `{name}` | {kind} | {line} | {sig_col} |")

        if not rows:
            lines.append("| *(none)* | | | | |")
        lines.append("")

        # --- 3. Dependencies: what these files import ---
        lines.append("### Dependencies (what changed files import)")
        lines.append("")

        dep_rows = self.db.execute(
            f"SELECT DISTINCT i.imported_module, i.imported_name "
            f"FROM imports i "
            f"WHERE i.file_id IN ({placeholders}) "
            f"AND i.imported_module != '' "
            f"ORDER BY i.imported_module",
            file_ids
        ).fetchall()

        # Group by module
        dep_modules: dict[str, list[str]] = {}
        for mod, name in dep_rows:
            dep_modules.setdefault(mod, [])
            if name:
                dep_modules[mod].append(name)

        # Show internal deps prominently, external as summary
        internal = {m: n for m, n in dep_modules.items()
                    if any(m.startswith(p) for p in ("app.", "common.", "app.vvp", "app.api", "app.keri", "app.db"))}
        external = {m: n for m, n in dep_modules.items() if m not in internal}

        if internal:
            lines.append("**Internal:**")
            for mod, names in sorted(internal.items()):
                if names:
                    lines.append(f"- `{mod}` → {', '.join(f'`{n}`' for n in names[:5])}"
                                 + (f" (+{len(names)-5} more)" if len(names) > 5 else ""))
                else:
                    lines.append(f"- `{mod}`")

        if external:
            ext_list = sorted(external.keys())
            lines.append(f"\n**External:** {', '.join(f'`{m}`' for m in ext_list[:15])}"
                         + (f" (+{len(ext_list)-15} more)" if len(ext_list) > 15 else ""))
        lines.append("")

        # --- 4. Reverse dependencies: what imports these files ---
        lines.append("### Reverse Dependencies (what imports the changed files)")
        lines.append("")

        # Build module patterns to search for
        # Source files use short imports like "app.api.credential" but the files
        # table stores full paths like "services.issuer.app.api.credential".
        # We need to generate both forms.
        module_patterns = []
        for mod in file_modules:
            if mod:
                module_patterns.append(mod)
                # Strip leading "services.{service}." or "common.common." prefix
                # to match how imports appear in source code
                for prefix in ("services.verifier.", "services.issuer.",
                               "services.keri-agent.", "services.sip-redirect.",
                               "services.sip-verify.", "common.common."):
                    if mod.startswith(prefix):
                        short = mod[len(prefix):]
                        module_patterns.append(short)
                        # Also add parent (for "from app.api import credential")
                        parent = short.rsplit(".", 1)
                        if len(parent) == 2:
                            module_patterns.append(parent[0])
                        break
                else:
                    # Also match partial imports
                    parts = mod.rsplit(".", 1)
                    if len(parts) == 2:
                        module_patterns.append(parts[0])
        # Deduplicate
        module_patterns = list(dict.fromkeys(module_patterns))

        if module_patterns:
            mod_placeholders = ",".join("?" * len(module_patterns))
            rev_rows = self.db.execute(
                f"SELECT DISTINCT f.path, i.imported_module, i.imported_name "
                f"FROM imports i JOIN files f ON i.file_id = f.id "
                f"WHERE i.imported_module IN ({mod_placeholders}) "
                f"AND i.file_id NOT IN ({placeholders}) "
                f"ORDER BY f.path",
                module_patterns + file_ids
            ).fetchall()

            if rev_rows:
                # Group by file
                rev_by_file: dict[str, list[str]] = {}
                for path, mod, name in rev_rows:
                    rev_by_file.setdefault(path, [])
                    if name:
                        rev_by_file[path].append(name)

                for path, names in sorted(rev_by_file.items()):
                    short = path.split("/", 3)[-1] if "/" in path else path
                    if names:
                        lines.append(f"- `{short}` uses {', '.join(f'`{n}`' for n in names[:4])}"
                                     + (f" (+{len(names)-4})" if len(names) > 4 else ""))
                    else:
                        lines.append(f"- `{short}`")

                # Limit output
                if len(rev_by_file) > 20:
                    lines.append(f"  *(showing 20 of {len(rev_by_file)} dependent files)*")
            else:
                lines.append("*(no other files import these modules)*")
        else:
            lines.append("*(could not resolve module names)*")
        lines.append("")

        # --- 5. Endpoints in/near target files ---
        # Include endpoints from the changed files AND from files in the same package
        lines.append("### Endpoints in Affected Packages")
        lines.append("")

        # Get packages of changed files
        packages = set()
        for fid in file_ids:
            pkg = self.db.execute("SELECT package FROM files WHERE id = ?", (fid,)).fetchone()
            if pkg:
                packages.add(pkg[0])

        # Get the directories of changed files for more targeted matching
        changed_dirs = set()
        for fp in file_paths:
            parts = fp.rsplit("/", 1)
            if len(parts) == 2:
                changed_dirs.add(parts[0])

        endpoint_rows = self.db.execute(
            f"SELECT e.method, e.path, e.handler, e.response_model, f.path as file "
            f"FROM endpoints e JOIN files f ON e.file_id = f.id "
            f"WHERE e.file_id IN ({placeholders}) "
            f"ORDER BY e.path",
            file_ids
        ).fetchall()

        if endpoint_rows:
            lines.append("| Method | Path | Handler | Response Model |")
            lines.append("|--------|------|---------|----------------|")
            for method, path, handler, resp, _ in endpoint_rows:
                lines.append(f"| {method} | `{path}` | `{handler}` | {resp or ''} |")
        else:
            # Show nearby endpoints from same directory
            if changed_dirs:
                dir_conditions = " OR ".join("f.path LIKE ?" for _ in changed_dirs)
                dir_patterns = [d + "/%" for d in changed_dirs]
                nearby = self.db.execute(
                    f"SELECT e.method, e.path, e.handler "
                    f"FROM endpoints e JOIN files f ON e.file_id = f.id "
                    f"WHERE {dir_conditions} "
                    f"ORDER BY e.path LIMIT 20",
                    dir_patterns
                ).fetchall()
                if nearby:
                    lines.append(f"*No endpoints in changed files. Nearby endpoints in same directory:*")
                    lines.append("")
                    lines.append("| Method | Path | Handler |")
                    lines.append("|--------|------|---------|")
                    for method, path, handler in nearby:
                        lines.append(f"| {method} | `{path}` | `{handler}` |")
                else:
                    lines.append("*(no endpoints in or near changed files)*")
            else:
                lines.append("*(no endpoints in changed files)*")
        lines.append("")

        # --- 6. Models in/near target files ---
        lines.append("### Models in Changed Files")
        lines.append("")

        model_rows = self.db.execute(
            f"SELECT m.name, m.kind, m.table_name, f.path "
            f"FROM models m JOIN files f ON m.file_id = f.id "
            f"WHERE m.file_id IN ({placeholders}) "
            f"ORDER BY f.path, m.line",
            file_ids
        ).fetchall()

        if model_rows:
            for mname, mkind, tablename, mpath in model_rows:
                table_note = f" (table: `{tablename}`)" if tablename else ""
                lines.append(f"- **`{mname}`** ({mkind}){table_note} — `{mpath.split('/')[-1]}`")

                # Show fields for this model
                mid = self.db.execute(
                    "SELECT id FROM models WHERE name = ? AND file_id IN "
                    f"(SELECT id FROM files WHERE path = ?)",
                    (mname, mpath)
                ).fetchone()
                if mid:
                    fields = self.db.execute(
                        "SELECT name, type_hint FROM model_fields WHERE model_id = ? ORDER BY line",
                        (mid[0],)
                    ).fetchall()
                    if fields:
                        field_strs = [f"`{fn}`:{ft}" if ft else f"`{fn}`" for fn, ft in fields[:10]]
                        lines.append(f"  Fields: {', '.join(field_strs)}"
                                     + (f" (+{len(fields)-10})" if len(fields) > 10 else ""))
        else:
            lines.append("*(no models in changed files)*")
        lines.append("")

        # --- 7. Test coverage ---
        lines.append("### Test Coverage for Changed Files")
        lines.append("")

        # Find tests that import from the changed modules
        test_rows = []
        if module_patterns:
            mod_placeholders2 = ",".join("?" * len(module_patterns))
            test_rows = self.db.execute(
                f"SELECT DISTINCT f.path, t.name, t.parent_class "
                f"FROM tests t "
                f"JOIN files f ON t.file_id = f.id "
                f"WHERE t.file_id IN ("
                f"  SELECT DISTINCT i.file_id FROM imports i "
                f"  WHERE i.imported_module IN ({mod_placeholders2})"
                f") "
                f"ORDER BY f.path, t.parent_class, t.name "
                f"LIMIT 30",
                module_patterns
            ).fetchall()

        # Also include tests directly in changed files
        direct_tests = self.db.execute(
            f"SELECT f.path, t.name, t.parent_class "
            f"FROM tests t JOIN files f ON t.file_id = f.id "
            f"WHERE t.file_id IN ({placeholders}) "
            f"ORDER BY f.path, t.name",
            file_ids
        ).fetchall()

        all_tests = list({(p, n, c) for p, n, c in (test_rows + direct_tests)})
        all_tests.sort()

        if all_tests:
            # Group by file
            by_file: dict[str, list[str]] = {}
            for path, name, cls in all_tests:
                short = path.split("/")[-1]
                by_file.setdefault(short, [])
                label = f"{cls}.{name}" if cls else name
                by_file[short].append(label)

            for tfile, tnames in sorted(by_file.items()):
                lines.append(f"- **{tfile}** ({len(tnames)} tests)")
                for tn in tnames[:5]:
                    lines.append(f"  - `{tn}`")
                if len(tnames) > 5:
                    lines.append(f"  - *...and {len(tnames)-5} more*")
        else:
            lines.append("*(no tests found that import from changed modules)*")
        lines.append("")

        return "\n".join(lines)

    def close(self):
        self.db.close()


# ---------------------------------------------------------------------------
# Drift checks (Sprint 125)
# ---------------------------------------------------------------------------

# Fixed SQL constants -- no user input is ever interpolated into these.
_STALE_EXPORTS_SQL = """
SELECT f.path, e.name
FROM file_header_exports e
JOIN files f ON f.id = e.file_id
WHERE NOT EXISTS (
    SELECT 1 FROM symbols s
    WHERE s.file_id = e.file_id
      AND s.name = e.name
      AND s.kind IN ('class', 'function', 'constant', 'enum')
)
ORDER BY f.path, e.name
"""

_STALE_DEPENDS_SQL = """
SELECT f.path, d.target
FROM file_header_depends d
JOIN files f ON f.id = d.file_id
WHERE d.scope = 'internal'
  AND NOT EXISTS (
    SELECT 1 FROM imports i
    WHERE i.file_id = d.file_id
      AND (i.imported_module = d.target
           OR i.imported_name = d.target
           OR i.imported_module LIKE d.target || '%')
)
ORDER BY f.path, d.target
"""


def _run_stale_exports() -> None:
    """Report files whose Exports list names symbols not present in the file."""
    idx = CodebaseIndexer(DB_PATH)
    rows = idx.db.execute(_STALE_EXPORTS_SQL).fetchall()
    if not rows:
        print("stale-exports: OK -- no drift detected")
    else:
        print(f"stale-exports: {len(rows)} drift finding(s):")
        for path, name in rows:
            print(f"  {path}: Exports names '{name}' but no matching symbol exists")
    idx.close()


def _run_stale_depends() -> None:
    """Report files whose Depends-on list names modules not imported."""
    idx = CodebaseIndexer(DB_PATH)
    rows = idx.db.execute(_STALE_DEPENDS_SQL).fetchall()
    if not rows:
        print("stale-depends: OK -- no drift detected")
    else:
        print(f"stale-depends: {len(rows)} drift finding(s):")
        for path, target in rows:
            print(f"  {path}: declares internal dep on '{target}' but does not import it")
    idx.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_CONTEXT_MAX_LINES = 400


def _parse_max_lines_arg(argv: list[str]) -> int:
    """Parse ``--max-lines N`` (Sprint 6). Default 400. Zero or
    negative values are rejected with exit 2 — truncation is always
    at least 1 line deep."""
    if "--max-lines" not in argv:
        return DEFAULT_CONTEXT_MAX_LINES
    i = argv.index("--max-lines")
    if i + 1 >= len(argv):
        print("--max-lines requires a positive integer", file=sys.stderr)
        sys.exit(2)
    try:
        n = int(argv[i + 1])
    except ValueError:
        print(f"--max-lines must be an integer, got {argv[i + 1]!r}",
              file=sys.stderr)
        sys.exit(2)
    if n <= 0:
        print(f"--max-lines must be positive (got {n})", file=sys.stderr)
        sys.exit(2)
    return n


def render_context_table(body: str, max_lines: int) -> str:
    """Truncate the rendered context pack to ``max_lines`` lines.

    Sprint 6 (R2 #13): extracted from the CLI branch so the boundary
    behaviour is testable without subprocess / stdout capture.

    - At the boundary (line count == max_lines): no marker appended.
    - Above the boundary: keep the first ``max_lines`` lines and
      append a single truncation notice line.
    """
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body
    omitted = len(lines) - max_lines
    head = "\n".join(lines[:max_lines])
    marker = (
        f"... ({omitted} line{'s' if omitted != 1 else ''} omitted; "
        f"re-run with --max-lines N to see more)"
    )
    return head + "\n" + marker


def main():
    if "--context-for" in sys.argv:
        ci = sys.argv.index("--context-for")
        # Remaining args are file paths; skip flag tokens AND the
        # immediate value of any value-taking flag (--max-lines).
        tail = sys.argv[ci + 1:]
        file_paths: list[str] = []
        skip_next = False
        for a in tail:
            if skip_next:
                skip_next = False
                continue
            if a == "--max-lines":
                skip_next = True
                continue
            if a.startswith("--"):
                continue
            file_paths.append(a)
        if not file_paths:
            print("Usage: --context-for file1.py file2.py ...")
            sys.exit(1)
        max_lines = _parse_max_lines_arg(sys.argv)
        idx = CodebaseIndexer(DB_PATH)
        body = idx.generate_context(file_paths)
        print(render_context_table(body, max_lines))
        idx.close()
        return

    if "--stats" in sys.argv:
        idx = CodebaseIndexer(DB_PATH)
        idx.print_stats()
        idx.close()
        return

    if "--query" in sys.argv:
        qi = sys.argv.index("--query")
        sql = sys.argv[qi + 1] if qi + 1 < len(sys.argv) else ""
        if not sql:
            print("Usage: --query \"SELECT ...\"")
            return
        idx = CodebaseIndexer(DB_PATH)
        idx.run_query(sql)
        idx.close()
        return

    if "--stale-exports" in sys.argv:
        _run_stale_exports()
        return

    if "--stale-depends" in sys.argv:
        _run_stale_depends()
        return

    incremental = "--incremental" in sys.argv
    quiet = "--quiet" in sys.argv

    if not quiet:
        print("🔍 Indexing VVP codebase..." + (" (incremental)" if incremental else ""))

    start = time.time()
    idx = CodebaseIndexer(DB_PATH)
    stats = idx.index_all(incremental=incremental)
    elapsed = time.time() - start

    if stats is None:
        # Incremental mode, nothing changed
        if not quiet:
            print("✅ No changes detected")
        idx.close()
        return

    if not quiet:
        print(f"✅ Indexed in {elapsed:.1f}s")
        idx.print_stats()
    idx.close()


if __name__ == "__main__":
    main()

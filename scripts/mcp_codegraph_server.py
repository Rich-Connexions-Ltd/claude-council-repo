"""File: scripts/mcp_codegraph_server.py

Purpose: Standalone stdio MCP server exposing the seven codegraph_* tools
backed by `.claude/codebase.db`. Invoked by Claude Code via `.mcp.json`
on session start.

Role:
  Self-contained FastMCP server. Gives Claude Code structured access to
  the project's codegraph without resorting to shell invocations of
  `scripts/index-codebase.py --query`. All tools are read-only.

Exports:
  - build_server -- construct a FastMCP with the codegraph tools registered
  - main         -- CLI entry point (stdio transport)

Depends on:
  - external: fastmcp (MCP server framework), sqlite3 (stdlib)

Invariants & gotchas:
  - Read-only SQLite access via `file:<path>?mode=ro` URI.
  - `codegraph_query` exposes arbitrary SELECT deliberately: the DB is a
    local dev file with no network surface. Non-SELECT statements are
    rejected at the tool layer (defence in depth, not a security fence).
  - DB path resolution: CODEGRAPH_DB env var first, then walk upward from
    CWD looking for `.claude/codebase.db`, else a deterministic fallback
    that yields a clear "run the indexer" error.

Last updated: Sprint 1 (2026-04-14) -- initial standalone server
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP


CODEGRAPH_DB_ENV = "CODEGRAPH_DB"  # Override the DB path at runtime.


def _resolve_db_path() -> Path:
    env = os.environ.get(CODEGRAPH_DB_ENV)
    if env:
        return Path(env).expanduser()
    cur = Path.cwd()
    for parent in [cur, *cur.parents]:
        candidate = parent / ".claude" / "codebase.db"
        if candidate.exists():
            return candidate
    return Path.cwd() / ".claude" / "codebase.db"


def _open_db() -> sqlite3.Connection | None:
    db_path = _resolve_db_path()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(cursor: sqlite3.Cursor, rows: list) -> list[dict[str, Any]]:
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


def _error(message: str, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": True, "message": message}
    if details:
        out["details"] = details
    return out


def _db_missing() -> dict[str, Any]:
    return _error(
        "codegraph database not found. Run: "
        "`python3 scripts/index-codebase.py` from the repo root to build it, "
        f"or set {CODEGRAPH_DB_ENV} to an existing codebase.db path."
    )


_STALE_EXPORTS_SQL = (
    "SELECT f.path, e.name "
    "FROM file_header_exports e JOIN files f ON f.id = e.file_id "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM symbols s "
    "  WHERE s.file_id = e.file_id AND s.name = e.name "
    "    AND s.kind IN ('class', 'function', 'constant', 'enum')"
    ") ORDER BY f.path, e.name"
)

_STALE_DEPENDS_SQL = (
    "SELECT f.path, d.target "
    "FROM file_header_depends d JOIN files f ON f.id = d.file_id "
    "WHERE d.scope = 'internal' AND NOT EXISTS ("
    "  SELECT 1 FROM imports i WHERE i.file_id = d.file_id AND ("
    "    i.imported_module = d.target OR i.imported_name = d.target "
    "    OR i.imported_module LIKE d.target || '%'"
    "  )"
    ") ORDER BY f.path, d.target"
)


def build_server() -> FastMCP:
    mcp = FastMCP(name="codegraph", version="1.0.0")

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_stats() -> dict[str, Any]:
        """Return row counts for every indexed table and the DB path/size."""
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            tables = [
                "files", "symbols", "imports", "endpoints", "models",
                "model_fields", "tests", "file_headers",
                "file_header_exports", "file_header_depends",
            ]
            counts: dict[str, int] = {}
            for t in tables:
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    counts[t] = -1
            db_path = _resolve_db_path()
            return {
                "db_path": str(db_path),
                "db_size_kb": db_path.stat().st_size // 1024,
                "counts": counts,
            }
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_query(
        sql: Annotated[str, "A SELECT (or WITH ... SELECT) statement."],
        limit: Annotated[int, "Max rows (default 200, max 2000)"] = 200,
    ) -> dict[str, Any]:
        """Run an arbitrary SELECT query against the codegraph DB.

        Tables and key columns:
          files(id, path, module, package, lines)
          symbols(file_id, name, kind, line, signature, docstring, parent, bases)
          imports(file_id, imported_module, imported_name, alias, line)
          endpoints(file_id, method, path, handler, response_model)
          models(file_id, name, kind, table_name, bases)
          model_fields(model_id, name, type_hint, description)
          tests(file_id, name, kind, parent_class)
          file_headers(file_id, purpose, role, invariants, related,
                       last_updated_sprint, last_updated_date,
                       last_updated_message, comment_style)
          file_header_exports(file_id, name, description)
          file_header_depends(file_id, scope, target, reason)
        """
        stripped = sql.lstrip().upper()
        if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
            return _error(
                "Only SELECT (or WITH ... SELECT) statements are allowed. "
                f"Got: {sql[:40]!r}"
            )
        limit = max(1, min(int(limit), 2000))
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limit)
            return {
                "row_count": len(rows),
                "columns": [c[0] for c in cursor.description] if cursor.description else [],
                "rows": _rows_to_dicts(cursor, rows),
                "truncated": len(rows) == limit,
            }
        except sqlite3.Error as e:
            return _error(f"SQL error: {e}")
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_file_header(
        path: Annotated[str, "Repo-relative path (e.g. src/foo/bar.py)"],
    ) -> dict[str, Any]:
        """Return a single file's structured header as JSON."""
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            file_row = conn.execute(
                "SELECT id, path, module, lines FROM files WHERE path = ?", (path,)
            ).fetchone()
            if file_row is None:
                return _error(f"file not found in codegraph: {path!r}")
            file_id = file_row["id"]
            hdr = conn.execute(
                "SELECT purpose, role, invariants, related, comment_style, "
                "last_updated_sprint, last_updated_date, last_updated_message, "
                "parse_errors "
                "FROM file_headers WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if hdr is None:
                return {
                    "path": path,
                    "has_header": False,
                    "message": "no header block indexed for this file",
                }
            exports = conn.execute(
                "SELECT name, description FROM file_header_exports WHERE file_id = ?",
                (file_id,),
            ).fetchall()
            depends = conn.execute(
                "SELECT scope, target, reason FROM file_header_depends WHERE file_id = ?",
                (file_id,),
            ).fetchall()
            parse_errors = None
            if hdr["parse_errors"]:
                try:
                    parse_errors = _json.loads(hdr["parse_errors"])
                except (ValueError, TypeError):
                    parse_errors = [hdr["parse_errors"]]
            return {
                "path": path,
                "module": file_row["module"],
                "lines": file_row["lines"],
                "has_header": True,
                "purpose": hdr["purpose"],
                "role": hdr["role"],
                "invariants": hdr["invariants"],
                "related": hdr["related"],
                "comment_style": hdr["comment_style"],
                "last_updated": {
                    "sprint": hdr["last_updated_sprint"],
                    "date": hdr["last_updated_date"],
                    "message": hdr["last_updated_message"],
                },
                "exports": [dict(r) for r in exports],
                "depends": [dict(r) for r in depends],
                "parse_errors": parse_errors,
            }
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_search_headers(
        purpose: Annotated[str, "Substring to match in Purpose. Empty = no filter."] = "",
        role: Annotated[str, "Substring to match in Role. Empty = no filter."] = "",
        invariants: Annotated[str, "Substring to match in Invariants. Empty = no filter."] = "",
        limit: Annotated[int, "Max rows (default 50, max 500)"] = 50,
    ) -> dict[str, Any]:
        """Find files whose header Purpose/Role/Invariants match substrings."""
        limit = max(1, min(int(limit), 500))
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if purpose:
                clauses.append("LOWER(h.purpose) LIKE ?")
                params.append(f"%{purpose.lower()}%")
            if role:
                clauses.append("LOWER(h.role) LIKE ?")
                params.append(f"%{role.lower()}%")
            if invariants:
                clauses.append("LOWER(h.invariants) LIKE ?")
                params.append(f"%{invariants.lower()}%")
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            sql = (
                "SELECT f.path, h.purpose, h.role "
                "FROM file_headers h JOIN files f ON f.id = h.file_id "
                f"{where} ORDER BY f.path LIMIT ?"
            )
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return {"row_count": len(rows), "rows": [dict(r) for r in rows]}
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_context_for(
        paths: Annotated[list[str], "Repo-relative file paths"],
        header_only: Annotated[bool, "If true, only header summaries."] = False,
    ) -> dict[str, Any]:
        """Return a multi-file context summary.

        In header_only mode, only the structured header per file. Otherwise
        includes up to 50 symbols and all endpoints per file.
        """
        if not isinstance(paths, list) or not paths:
            return _error("paths must be a non-empty list of strings")
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            result: list[dict[str, Any]] = []
            for p in paths:
                file_row = conn.execute(
                    "SELECT id, path, module, lines FROM files WHERE path = ?",
                    (p,),
                ).fetchone()
                if file_row is None:
                    result.append({"path": p, "error": "not indexed"})
                    continue
                file_id = file_row["id"]
                entry: dict[str, Any] = {
                    "path": p,
                    "module": file_row["module"],
                    "lines": file_row["lines"],
                }
                hdr = conn.execute(
                    "SELECT purpose, role FROM file_headers WHERE file_id = ?",
                    (file_id,),
                ).fetchone()
                if hdr:
                    entry["purpose"] = hdr["purpose"]
                    entry["role"] = hdr["role"]
                if not header_only:
                    sym_rows = conn.execute(
                        "SELECT name, kind, line FROM symbols WHERE file_id = ? "
                        "ORDER BY line LIMIT 50",
                        (file_id,),
                    ).fetchall()
                    entry["symbols"] = [dict(r) for r in sym_rows]
                    ep_rows = conn.execute(
                        "SELECT method, path, handler FROM endpoints WHERE file_id = ? "
                        "ORDER BY method, path",
                        (file_id,),
                    ).fetchall()
                    if ep_rows:
                        entry["endpoints"] = [dict(r) for r in ep_rows]
                result.append(entry)
            return {"files": result, "header_only": header_only}
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_stale_exports() -> dict[str, Any]:
        """Files whose Exports: lists a symbol that doesn't exist in the file."""
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            rows = conn.execute(_STALE_EXPORTS_SQL).fetchall()
            return {"finding_count": len(rows), "findings": [dict(r) for r in rows]}
        finally:
            conn.close()

    @mcp.tool(annotations={"readOnlyHint": True})
    def codegraph_stale_depends() -> dict[str, Any]:
        """Files whose internal Depends on: names a module not actually imported."""
        conn = _open_db()
        if conn is None:
            return _db_missing()
        try:
            rows = conn.execute(_STALE_DEPENDS_SQL).fetchall()
            return {"finding_count": len(rows), "findings": [dict(r) for r in rows]}
        finally:
            conn.close()

    return mcp


def main() -> None:
    server = build_server()
    server.run()  # Defaults to stdio transport.


if __name__ == "__main__":
    main()

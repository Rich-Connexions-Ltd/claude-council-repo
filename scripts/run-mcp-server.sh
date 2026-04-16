#!/bin/bash
# File: scripts/run-mcp-server.sh
# Purpose: Launch the stdio MCP server that exposes codegraph_* tools
# against the project's .claude/codebase.db. Invoked by Claude Code via
# .mcp.json on session start.
# Last updated: Sprint 1 (2026-04-14) -- initial launcher
set -e

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/scripts"
exec python3 -m scripts.mcp_codegraph_server "$@"

#!/usr/bin/env python3
"""File: scripts/hooks/guardrails.py

Purpose: PreToolUse hook for Bash tool calls. Emits advisory
speed-bumps for a small set of command patterns that commonly cause
regret (--no-verify, force-push, rm -rf outside allow-list).

Role:
  Advisory, not enforcement. A determined caller can bypass these
  rules via shell aliases, eval, subprocess, or by toggling
  COUNCIL_HOOK_PROFILE=off. Purpose is to add friction to impulsive
  destructive actions and surface nudges in stderr.

Exports:
  - main() -- hook entrypoint reading JSON from stdin

Depends on:
  - external: python stdlib only (json, sys, os, shlex, pathlib)

Invariants & gotchas:
  - Fail-open on malformed input: exit 0 if stdin is not valid JSON.
  - Mode (COUNCIL_HOOK_PROFILE): off (exit 0), warn (exit 0 + stderr),
    strict (exit 2 blocks tool call). Default: warn.
  - rm -rf uses Path.resolve() canonical path; relative paths resolve
    against cwd from the hook payload.

Last updated: Sprint 1 (2026-04-15) -- initial implementation
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STOPWORDS_NONE: set[str] = set()


@dataclass
class RuleMatch:
    matched: bool
    message: str = ""


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def rule_no_verify(tokens: list[str], cwd: Path) -> RuleMatch:
    if len(tokens) < 2:
        return RuleMatch(False)
    if tokens[0] != "git" or tokens[1] != "commit":
        return RuleMatch(False)
    if "--no-verify" in tokens:
        return RuleMatch(
            True,
            "git commit --no-verify skips pre-commit hooks. "
            "Fix the hook failure instead of bypassing it.",
        )
    return RuleMatch(False)


def rule_force_push(tokens: list[str], cwd: Path) -> RuleMatch:
    if len(tokens) < 2:
        return RuleMatch(False)
    if tokens[0] != "git" or tokens[1] != "push":
        return RuleMatch(False)
    force_flags = {"--force", "--force-with-lease", "-f"}
    if any(t in force_flags or t.startswith("--force=") for t in tokens):
        return RuleMatch(
            True,
            "git push --force can overwrite remote history. "
            "Prefer --force-with-lease and confirm the branch.",
        )
    return RuleMatch(False)


def _rm_rf_allowed(target: Path, cwd: Path) -> bool:
    try:
        resolved = (cwd / target).resolve() if not target.is_absolute() else target.resolve()
    except (OSError, RuntimeError):
        return False
    s = str(resolved)
    allow_roots = [Path("/tmp").resolve(), Path("/private/tmp").resolve()]
    for root in allow_roots:
        try:
            if resolved.is_relative_to(root):
                return True
        except (ValueError, AttributeError):
            if s.startswith(str(root) + os.sep) or s == str(root):
                return True
    try:
        repo_root = cwd.resolve()
    except OSError:
        return False
    allowed_names = {"node_modules", "dist", "build", ".next", "target"}
    try:
        rel = resolved.relative_to(repo_root)
        parts = rel.parts
        if parts and parts[0] in allowed_names:
            return True
        if parts and parts[0] == ".claude":
            if any(p.startswith("codebase.db") for p in parts[1:]):
                return True
    except ValueError:
        pass
    return False


def rule_rm_rf(tokens: list[str], cwd: Path) -> RuleMatch:
    if not tokens or tokens[0] != "rm":
        return RuleMatch(False)
    rf_flags = {"-rf", "-fr", "-Rf", "-fR", "-rfv", "-fvr"}
    has_rf = any(t in rf_flags for t in tokens[1:])
    if not has_rf:
        flag_chars: set[str] = set()
        for t in tokens[1:]:
            if t.startswith("-") and not t.startswith("--"):
                flag_chars.update(t.lstrip("-"))
        if {"r", "f"}.issubset(flag_chars) or {"R", "f"}.issubset(flag_chars):
            has_rf = True
    if not has_rf:
        return RuleMatch(False)
    targets = [t for t in tokens[1:] if not t.startswith("-")]
    if not targets:
        return RuleMatch(False)
    for target in targets:
        if not _rm_rf_allowed(Path(target), cwd):
            return RuleMatch(
                True,
                f"rm -rf target '{target}' resolves outside the allow-list "
                f"(/tmp, node_modules/, dist/, build/, .claude/codebase.db*). "
                f"Double-check before proceeding.",
            )
    return RuleMatch(False)


RULES: list[Callable[[list[str], Path], RuleMatch]] = [
    rule_no_verify,
    rule_force_push,
    rule_rm_rf,
]


def _mode() -> str:
    raw = os.environ.get("COUNCIL_HOOK_PROFILE", "warn").strip().lower()
    if raw not in {"off", "warn", "strict"}:
        print(
            f"guardrails: unknown COUNCIL_HOOK_PROFILE={raw!r}; treating as 'warn'",
            file=sys.stderr,
        )
        return "warn"
    return raw


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool = payload.get("tool_name") or payload.get("tool") or ""
    if tool != "Bash":
        return 0

    tool_input = payload.get("tool_input") or payload.get("input") or {}
    command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        return 0

    cwd = Path(payload.get("cwd") or os.getcwd())
    tokens = _tokens(command)

    mode = _mode()
    if mode == "off":
        return 0

    matched_messages: list[str] = []
    for rule in RULES:
        try:
            result = rule(tokens, cwd)
        except Exception as exc:
            print(f"guardrails: rule {rule.__name__} errored: {exc}", file=sys.stderr)
            continue
        if result.matched:
            matched_messages.append(result.message)

    if not matched_messages:
        return 0

    header = "⚠  Guardrail advisory:" if mode == "warn" else "✖  Guardrail (strict):"
    for msg in matched_messages:
        print(f"{header} {msg}", file=sys.stderr)

    return 2 if mode == "strict" else 0


if __name__ == "__main__":
    sys.exit(main())

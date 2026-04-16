#!/usr/bin/env python3
"""File: scripts/profile.py

Purpose: Single source of truth for reading the active project profile
and determining which components are enabled.

Role:
  Every consumer (bootstrap.py, council-review.py, archive-plan.sh via
  CLI, findings-digest.py, tests) reads profile membership through
  this module. profiles.json defines the profile -> components
  mapping; .claude/project-profile persists the chosen profile for a
  bootstrapped project.

Exports:
  - load_profile(repo_root) -> str
  - is_enabled(component, repo_root) -> bool
  - load_profiles_json(repo_root) -> dict
  - CLI entrypoint: python3 scripts/profile.py is-enabled <component>

Depends on:
  - external: python stdlib only (json, pathlib, sys, os)

Invariants & gotchas:
  - Absent .claude/project-profile -> returns default profile from
    profiles.json (logged once). Malformed / unknown profile name ->
    warns on stderr, returns default.
  - CLI exit codes: 0 = component enabled, 1 = not enabled, 2 = error.

Last updated: Sprint 1 (2026-04-15) -- initial implementation
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
PROFILES_JSON = "scripts/bootstrap/profiles.json"
PROFILE_FILE = ".claude/project-profile"

_absent_warned = False


def load_profiles_json(repo_root: Path | None = None) -> dict:
    root = repo_root or REPO_ROOT_DEFAULT
    path = root / PROFILES_JSON
    with path.open() as f:
        return json.load(f)


def load_profile(repo_root: Path | None = None) -> str:
    global _absent_warned
    root = repo_root or REPO_ROOT_DEFAULT
    profiles = load_profiles_json(root)
    default = profiles.get("default", "standard")
    profile_path = root / PROFILE_FILE

    if not profile_path.exists():
        if not _absent_warned:
            print(
                f"profile.py: {PROFILE_FILE} absent; defaulting to '{default}'",
                file=sys.stderr,
            )
            _absent_warned = True
        return default

    try:
        with profile_path.open() as f:
            data = json.load(f)
        name = data.get("profile")
        if not isinstance(name, str) or name not in profiles["profiles"]:
            print(
                f"profile.py: {PROFILE_FILE} has unknown profile {name!r}; "
                f"defaulting to '{default}'",
                file=sys.stderr,
            )
            return default
        return name
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"profile.py: failed to read {PROFILE_FILE} ({exc}); "
            f"defaulting to '{default}'",
            file=sys.stderr,
        )
        return default


def is_enabled(component: str, repo_root: Path | None = None) -> bool:
    root = repo_root or REPO_ROOT_DEFAULT
    profiles = load_profiles_json(root)
    name = load_profile(root)
    profile = profiles["profiles"].get(name)
    if profile is None:
        return False
    known_components: set[str] = set()
    for p in profiles["profiles"].values():
        known_components.update(p.get("components", []))
    if component not in known_components:
        raise KeyError(f"unknown component: {component!r}")
    return component in profile.get("components", [])


def _cli_is_enabled(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: profile.py is-enabled <component>", file=sys.stderr)
        return 2
    try:
        enabled = is_enabled(argv[0])
    except KeyError as exc:
        print(f"profile.py: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"profile.py: {exc}", file=sys.stderr)
        return 2
    return 0 if enabled else 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: profile.py <subcommand> [args]", file=sys.stderr)
        print("  is-enabled <component>", file=sys.stderr)
        return 2
    sub, *rest = argv
    if sub == "is-enabled":
        return _cli_is_enabled(rest)
    if sub == "current":
        print(load_profile())
        return 0
    print(f"profile.py: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

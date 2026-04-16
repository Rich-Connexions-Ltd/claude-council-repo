"""File: tests/conftest.py

Purpose: Shared pytest fixtures for the test suite.

Last updated: Sprint 4 (2026-04-16) -- shared run_git helper, make_dev_repo/make_template_repo factories, _run_apply_profile uses env-passed paths (code review R1 findings #21, #23, #24).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(alias: str, path: Path):
    spec = importlib.util.spec_from_file_location(alias, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[alias] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


@pytest.fixture
def council_review_module():
    return _load_module("cr_mod", REPO_ROOT / "scripts" / "council-review.py")


@pytest.fixture
def findings_digest_module():
    return _load_module("fd_mod", REPO_ROOT / "scripts" / "findings-digest.py")


@pytest.fixture
def bootstrap_module():
    return _load_module("bs_mod", REPO_ROOT / "scripts" / "bootstrap.py")


@pytest.fixture
def profile_module():
    return _load_module("pm_mod", REPO_ROOT / "scripts" / "profile.py")


@pytest.fixture
def publish_template_module():
    return _load_module(
        "pt_mod", REPO_ROOT / "scripts" / "publish-template.py"
    )


GIT_TEST_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def run_git(
    cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Shared git helper for tests (finding #21 consolidation).

    Deterministic env — won't pick up the caller's git identity.
    """
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_TEST_ENV},
    )


def _init_git_repo(path: Path, *, branch: str = "main") -> None:
    """Initialise a git repo with a single commit on the named branch.

    Sprint 5: sets local user.email / user.name on the test repo so
    that production code (which runs bare ``git`` commands without
    our GIT_AUTHOR_EMAIL env injection) can still commit on CI. On a
    developer's laptop the global gitconfig masks the missing local
    identity; GitHub Actions runners have no global config.
    """
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-b", branch)
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("initial\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-m", "init")


@pytest.fixture
def make_template_repo(tmp_path):
    """Factory: create an empty git-initialised template-repo at tmp_path/template."""

    def _make(branch: str = "main") -> Path:
        path = tmp_path / "template"
        _init_git_repo(path, branch=branch)
        return path

    return _make


@pytest.fixture
def make_dev_repo(tmp_path):
    """Factory: create a minimal dev-container with a manifest + included files."""

    def _make(
        include: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        seeded_files: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
    ) -> Path:
        dev = tmp_path / "dev"
        dev.mkdir()
        default_files = {
            "scripts/hello.py": '"""hello module"""\nprint("hi")\n',
            "README.md": "# dev\n",
            "scripts/template-seeds/SPRINTS.md": "# seeded sprints\n",
        }
        default_files.update(files or {})
        for rel, content in default_files.items():
            dest = dev / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

        manifest = {
            "version": 1,
            "description": "test manifest",
            "include": include or ["scripts/", "README.md"],
            "exclude_patterns": exclude_patterns
            if exclude_patterns is not None
            else ["**/__pycache__/**", "**/*.pyc"],
            "seeded_files": seeded_files
            if seeded_files is not None
            else {"SPRINTS.md": "scripts/template-seeds/SPRINTS.md"},
        }
        (dev / "manifest.json").write_text(json.dumps(manifest, indent=2))
        _init_git_repo(dev)
        return dev

    return _make


def _copy_template(tmp_path: Path) -> Path:
    """Copy just the files bootstrap's profile logic needs into tmp."""
    dest = tmp_path / "proj"
    dest.mkdir()
    (dest / "scripts" / "bootstrap").mkdir(parents=True)
    (dest / "scripts" / "hooks").mkdir(parents=True)
    (dest / "scripts" / "indexers").mkdir(parents=True)
    (dest / ".claude" / "skills").mkdir(parents=True)
    # Copy profile infrastructure
    for rel in [
        "scripts/profile.py",
        "scripts/bootstrap/profiles.json",
        "scripts/bootstrap/component_files.json",
        "scripts/bootstrap/settings.template.json",
    ]:
        shutil.copy2(REPO_ROOT / rel, dest / rel)
    # Copy component files that profile tests check for presence/absence
    rels_to_copy = [
        "scripts/check-headers.py", "scripts/header_parser.py", "scripts/bump-header.py",
        "scripts/index-codebase.py", "scripts/mcp_codegraph_server.py", "scripts/run-mcp-server.sh",
        ".mcp.json",
        "scripts/council-review.py", "scripts/council-config.json", "scripts/council-check.sh",
        "scripts/process-test.py",
        ".claude/skills/sprint-start.md", ".claude/skills/sprint-complete.md",
        ".claude/skills/check-headers.md", ".claude/skills/council-plan.md",
        ".claude/skills/council-code.md",
        "scripts/hooks/guardrails.py",
        "scripts/findings-digest.py",
    ]
    for rel in rels_to_copy:
        src = REPO_ROOT / rel
        if src.exists():
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for rel in ["scripts/indexers/__init__.py"]:
        src = REPO_ROOT / rel
        if src.exists():
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return dest


def _run_apply_profile(dest: Path, profile: str) -> None:
    """Run bootstrap.apply_profile with REPO_ROOT pointing at dest.

    Finding #23: paths are passed via env vars rather than interpolated
    into the exec'd source, to avoid quoting issues if a tmpdir path
    ever contains a stray quote or backslash.
    """
    bs_path = dest / "scripts" / "bootstrap.py"
    if not bs_path.exists():
        bs_path = REPO_ROOT / "scripts" / "bootstrap.py"
    code = textwrap.dedent("""
        import os, sys, importlib.util
        from pathlib import Path
        bs_path = Path(os.environ['_BS_PATH'])
        dest = Path(os.environ['_BS_DEST'])
        profile = os.environ['_BS_PROFILE']
        spec = importlib.util.spec_from_file_location('bs', bs_path)
        m = importlib.util.module_from_spec(spec)
        m.REPO_ROOT = dest
        sys.modules['bs'] = m
        spec.loader.exec_module(m)
        m.REPO_ROOT = dest
        m.apply_profile(profile)
    """).strip()
    env = {
        **os.environ,
        "_BS_PATH": str(bs_path),
        "_BS_DEST": str(dest),
        "_BS_PROFILE": profile,
    }
    subprocess.run(
        [sys.executable, "-c", code], check=True, cwd=dest, env=env
    )


@pytest.fixture
def bootstrap_minimal(tmp_path):
    dest = _copy_template(tmp_path)
    _run_apply_profile(dest, "minimal")
    return dest


@pytest.fixture
def bootstrap_standard(tmp_path):
    dest = _copy_template(tmp_path)
    _run_apply_profile(dest, "standard")
    return dest


@pytest.fixture
def bootstrap_full(tmp_path):
    dest = _copy_template(tmp_path)
    _run_apply_profile(dest, "full")
    return dest

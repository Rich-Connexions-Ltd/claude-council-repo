"""File: tests/test_check_headers.py

Purpose: Coverage for the Sprint 5 --changed-against fallback cascade:
default-ref → active sprint base commit → HEAD~1 → full-scan-with-
staleness-suppressed. Also covers the explicit-override error path,
where a user-supplied ref that doesn't resolve should fail loudly.

Last updated: Sprint 5 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CH_PATH = REPO_ROOT / "scripts" / "check-headers.py"


def _load_check_headers_module(repo_root: Path):
    """Load check-headers.py with REPO_ROOT rebound to the test repo."""
    spec = importlib.util.spec_from_file_location(
        f"ch_{repo_root.name}", CH_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.REPO_ROOT = repo_root
    return mod


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check, capture_output=True, text=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def repo_with_commits(tmp_path):
    """Git repo with two commits, no remote configured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1", "-q")
    (repo / "README.md").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2", "-q")
    return repo


def test_ref_exists_true_for_head(repo_with_commits):
    ch = _load_check_headers_module(repo_with_commits)
    assert ch._ref_exists("HEAD") is True
    assert ch._ref_exists("HEAD~1") is True


def test_ref_exists_false_for_missing(repo_with_commits):
    ch = _load_check_headers_module(repo_with_commits)
    assert ch._ref_exists("origin/main") is False
    assert ch._ref_exists("nonsense/nosuchref") is False


def test_resolve_returns_ref_when_present(repo_with_commits):
    ch = _load_check_headers_module(repo_with_commits)
    # HEAD resolves → we get HEAD back (no cascade)
    assert ch._resolve_changed_ref("HEAD", arg_was_default=True) == "HEAD"


def test_resolve_falls_back_to_sprint_base_commit(repo_with_commits, capsys):
    """When origin/main is missing and an active sprint base-commit
    marker points at a valid sha, the cascade picks it up."""
    ch = _load_check_headers_module(repo_with_commits)
    head_sha = _git(repo_with_commits, "rev-parse", "HEAD~1").stdout.strip()
    (repo_with_commits / ".sprint-base-commit-7").write_text(head_sha + "\n")

    resolved = ch._resolve_changed_ref("origin/main", arg_was_default=True)
    assert resolved == head_sha
    captured = capsys.readouterr()
    assert "sprint 7 base commit" in captured.err


def test_resolve_most_recent_sprint_wins(repo_with_commits, capsys):
    """If multiple .sprint-base-commit-N files exist, the highest-
    numbered sprint is preferred."""
    ch = _load_check_headers_module(repo_with_commits)
    head_sha = _git(repo_with_commits, "rev-parse", "HEAD~1").stdout.strip()
    (repo_with_commits / ".sprint-base-commit-3").write_text(head_sha + "\n")
    (repo_with_commits / ".sprint-base-commit-9").write_text(head_sha + "\n")

    ch._resolve_changed_ref("origin/main", arg_was_default=True)
    captured = capsys.readouterr()
    assert "sprint 9" in captured.err
    assert "sprint 3" not in captured.err


def test_resolve_skips_invalid_base_commit_marker(repo_with_commits, capsys):
    """Malformed sha in the marker → cascade continues to HEAD~1."""
    ch = _load_check_headers_module(repo_with_commits)
    (repo_with_commits / ".sprint-base-commit-5").write_text(
        "notasha" * 5 + "\n"
    )
    resolved = ch._resolve_changed_ref("origin/main", arg_was_default=True)
    # Cascade continued to HEAD~1
    assert resolved == "HEAD~1"
    captured = capsys.readouterr()
    assert "HEAD~1" in captured.err


def test_resolve_falls_back_to_head_tilde_one(repo_with_commits, capsys):
    """No markers, no origin/main → HEAD~1."""
    ch = _load_check_headers_module(repo_with_commits)
    resolved = ch._resolve_changed_ref("origin/main", arg_was_default=True)
    assert resolved == "HEAD~1"
    captured = capsys.readouterr()
    assert "HEAD~1" in captured.err


def test_resolve_returns_none_when_nothing_works(tmp_path, capsys):
    """Single-commit repo: HEAD~1 doesn't exist; no markers; no
    origin/main → return None so caller can suppress staleness."""
    repo = tmp_path / "single_commit"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    (repo / "README.md").write_text("only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1", "-q")

    ch = _load_check_headers_module(repo)
    assert ch._resolve_changed_ref(
        "origin/main", arg_was_default=True
    ) is None


def test_resolve_explicit_override_with_missing_ref_returns_none(
    repo_with_commits, capsys
):
    """User-supplied --changed-against must not silently cascade; we
    return None so the caller can surface an error."""
    ch = _load_check_headers_module(repo_with_commits)
    assert ch._resolve_changed_ref(
        "some/missing/ref", arg_was_default=False
    ) is None


def test_collect_issues_suppresses_staleness_when_cascade_empty(
    tmp_path, capsys
):
    """Single-commit repo + default --changed-against → collect_issues
    returns no stale-sprint issues and emits the 'skipping staleness'
    notice on stderr."""
    repo = tmp_path / "single"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    # A source file with a stale Last updated line — would normally
    # trigger a stale-sprint issue on sprint 9.
    src = repo / "hello.py"
    src.write_text(
        '"""File: hello.py\nPurpose: greet.\n'
        'Last updated: Sprint 1 (2026-01-01) -- initial.\n"""\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1", "-q")

    ch = _load_check_headers_module(repo)
    issues = ch.collect_issues(
        [src],
        sprint="9",
        changed_against="origin/main",
        arg_was_default=True,
    )
    stale = [i for i in issues if i.kind == "stale-sprint"]
    assert stale == [], (
        "With no usable diff ref, staleness must be suppressed"
    )
    captured = capsys.readouterr()
    assert "Skipping sprint staleness check" in captured.err


def test_collect_issues_uses_head_tilde_one_when_origin_missing(
    repo_with_commits, capsys
):
    """On a repo with >1 commit and no origin/main, the staleness
    check proceeds against HEAD~1 and fires on files changed since."""
    repo = repo_with_commits
    # Modify an existing file with a stale Last updated → must appear
    # in the diff from HEAD~1.
    src = repo / "hello.py"
    src.write_text(
        '"""File: hello.py\nPurpose: greet.\n'
        'Last updated: Sprint 1 (2026-01-01) -- initial.\n"""\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add hello.py", "-q")

    ch = _load_check_headers_module(repo)
    issues = ch.collect_issues(
        [src],
        sprint="5",
        changed_against="origin/main",
        arg_was_default=True,
    )
    stale = [i for i in issues if i.kind == "stale-sprint"]
    assert len(stale) == 1, (
        f"expected stale-sprint issue via HEAD~1 fallback, got: {issues}"
    )
    captured = capsys.readouterr()
    assert "HEAD~1" in captured.err


def test_collect_issues_origin_main_when_present(tmp_path):
    """When origin/main exists, cascade stays off and the ref is used
    directly."""
    repo = tmp_path / "with_origin"
    repo.mkdir()
    _git(repo, "init", "-b", "main", "-q")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1", "-q")
    # Fake up origin/main by creating the ref directly at HEAD.
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)

    src = repo / "hello.py"
    src.write_text(
        '"""File: hello.py\nPurpose: greet.\n'
        'Last updated: Sprint 1 (2026-01-01) -- initial.\n"""\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2", "-q")

    ch = _load_check_headers_module(repo)
    issues = ch.collect_issues(
        [src],
        sprint="5",
        changed_against="origin/main",
        arg_was_default=True,
    )
    stale = [i for i in issues if i.kind == "stale-sprint"]
    assert len(stale) == 1

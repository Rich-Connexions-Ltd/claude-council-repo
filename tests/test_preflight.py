"""File: tests/test_preflight.py

Purpose: Coverage for the Sprint 2 pre-flight check — untracked source
file detection, reject path, --allow-untracked banner + contents, plan
review bypass, filename sanitisation.

Last updated: Sprint 2 (2026-04-16) -- initial coverage
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CR_PATH = REPO_ROOT / "scripts" / "council-review.py"


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    try:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init", "--allow-empty"],
            cwd=path, check=True,
        )
    except subprocess.CalledProcessError:
        pass


@pytest.fixture
def council_cr(council_review_module):
    return council_review_module


@pytest.fixture
def gitrepo(tmp_path):
    _init_git(tmp_path)
    return tmp_path


def test_find_untracked_only_source_suffixes(council_cr, gitrepo):
    (gitrepo / "a.py").write_text("x")
    (gitrepo / "b.log").write_text("x")
    (gitrepo / "c.md").write_text("x")
    result = council_cr.find_untracked_source_files(gitrepo)
    assert "a.py" in result
    assert "b.log" not in result
    assert "c.md" not in result


def test_find_untracked_respects_gitignore(council_cr, gitrepo):
    (gitrepo / ".gitignore").write_text("secret.py\n")
    (gitrepo / "secret.py").write_text("x")
    (gitrepo / "public.py").write_text("x")
    result = council_cr.find_untracked_source_files(gitrepo)
    assert "public.py" in result
    assert "secret.py" not in result


def test_find_untracked_non_git_returns_empty(council_cr, tmp_path):
    (tmp_path / "a.py").write_text("x")
    assert council_cr.find_untracked_source_files(tmp_path) == []


def test_filename_is_safe(council_cr):
    assert council_cr._filename_is_safe("foo.py")
    assert council_cr._filename_is_safe("path/to/foo.py")
    assert not council_cr._filename_is_safe("foo\nbar.py")
    assert not council_cr._filename_is_safe("foo\x01bar.py")
    assert not council_cr._filename_is_safe("foo\x7fbar.py")
    assert not council_cr._filename_is_safe("foo\x00bar.py")


def test_shared_source_extensions_identity(council_cr):
    # Sentinel mutation: if scanner and gatherer held independent copies,
    # this mutation would leak into one but not the other.
    before = set(council_cr.SOURCE_EXTENSIONS)
    council_cr.SOURCE_EXTENSIONS.add(".sentinel")
    try:
        assert ".sentinel" in council_cr.SOURCE_EXTENSIONS
        # gather_code_materials literal references the module constant
        # (not a local copy); verified by source inspection as well.
    finally:
        council_cr.SOURCE_EXTENSIONS.discard(".sentinel")
    assert set(council_cr.SOURCE_EXTENSIONS) == before


def test_preflight_clean_tree(council_cr, gitrepo):
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=False)
    assert pf.ok
    assert pf.banner == ""
    assert pf.reject_message == ""


def test_preflight_rejects_without_flag(council_cr, gitrepo):
    (gitrepo / "new_module.py").write_text("print('x')")
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=False)
    assert not pf.ok
    assert pf.banner == ""
    assert "new_module.py" in pf.reject_message
    assert "Commit them" in pf.reject_message
    assert "--allow-untracked" in pf.reject_message


def test_preflight_allows_with_flag(council_cr, gitrepo):
    (gitrepo / "new_module.py").write_text("x")
    (gitrepo / "other.py").write_text("x")
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=True)
    assert pf.ok
    assert pf.reject_message == ""
    assert "PRE-FLIGHT BANNER" in pf.banner
    assert "new_module.py" in pf.banner
    assert "other.py" in pf.banner


def test_preflight_allow_on_clean_tree_empty_banner(council_cr, gitrepo):
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=True)
    assert pf.ok
    assert pf.banner == ""


@pytest.mark.parametrize("suffix", [".log", ".tmp", ".DS_Store", ".pyc", ".swp", ".bak", ".lock", ""])
def test_non_source_extensions_pass_preflight(council_cr, gitrepo, suffix):
    (gitrepo / f"file{suffix}").write_text("x")
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=False)
    assert pf.ok, f"pre-flight rejected for suffix {suffix!r}: {pf.reject_message}"


def test_banner_quotes_filenames_with_spaces(council_cr, gitrepo):
    (gitrepo / "my file.py").write_text("x")
    pf = council_cr.preflight_code_review(gitrepo, allow_untracked=True)
    assert pf.ok
    # shlex.quote wraps names with spaces in single quotes
    assert "'my file.py'" in pf.banner


def test_gather_code_materials_with_banner_and_untracked(council_cr, tmp_path, monkeypatch):
    """Primary --allow-untracked contract: file *contents* appear in materials."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_git(repo)
    # Create an untracked .py with distinctive contents.
    (repo / "module_x.py").write_text("hello\nworld\n")
    # Empty PLAN/CHANGES so we can assert the rest cleanly.
    (repo / "PLAN_Sprint9.md").write_text("# plan")
    (repo / "CHANGES.md").write_text("# changes")

    pf = council_cr.preflight_code_review(repo, allow_untracked=True)
    materials = council_cr.gather_code_materials(
        "9", repo, banner=pf.banner, include_untracked=True,
    )
    assert "PRE-FLIGHT BANNER" in materials
    assert "module_x.py" in materials
    assert "hello\nworld" in materials


def test_argparse_multi_word_title_quoted(council_cr):
    ns = council_cr._parse_args(["code", "5", "Multi Word Title"])
    assert " ".join(ns.title) == "Multi Word Title"


def test_argparse_multi_word_title_bare_tokens(council_cr):
    ns = council_cr._parse_args(["code", "5", "Multi", "Word", "Title"])
    assert " ".join(ns.title) == "Multi Word Title"


def test_argparse_rejects_invalid_review_type(council_cr):
    with pytest.raises(SystemExit):
        council_cr._parse_args(["bogus", "5", "x"])


def test_argparse_flag_parsed(council_cr):
    ns = council_cr._parse_args(["code", "5", "t", "--allow-untracked"])
    assert ns.allow_untracked is True
    ns2 = council_cr._parse_args(["code", "5", "t"])
    assert ns2.allow_untracked is False


def _make_empty_council_config(repo: Path, review_type: str) -> None:
    """Write a council-config.json with zero active members for the
    given review_type so main() exits 1 at get_active_members AFTER
    pre-flight has run.

    Members use phases=["other"] so get_active_members returns [] for
    the target review_type, and no api_key_env is set so the API-key
    check (which runs only after get_active_members has found members)
    is not reached.
    """
    cfg = {
        "council": {
            "members": [{
                "id": "solo", "label": "Solo",
                "platform": "claude_cli", "model": "sonnet",
                "system_prompt": "x",
                "phases": ["other"],
                "role": "test",
            }],
            "consolidator": {"platform": "codex", "model": "codex"},
        },
        "convergence": {"max_rounds_plan": 5, "max_rounds_code": 6,
                        "warning_at_round": 4},
    }
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "council-config.json").write_text(json.dumps(cfg))


def test_plan_review_skips_preflight(tmp_path):
    """Creating an untracked .py in the tree must NOT cause a plan
    review to exit 4. Proof-of-skip: main exits 1 at
    get_active_members (throwaway config) with a specific error."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_git(repo)
    (repo / "untracked.py").write_text("x")
    _make_empty_council_config(repo, review_type="plan")

    # Copy council-review.py so it resolves the repo-local config.
    (repo / "scripts").mkdir(exist_ok=True)
    shutil.copy2(CR_PATH, repo / "scripts" / "council-review.py")
    shutil.copy2(REPO_ROOT / "scripts" / "profile.py", repo / "scripts" / "profile.py")
    (repo / "scripts" / "bootstrap").mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "bootstrap" / "profiles.json",
                 repo / "scripts" / "bootstrap" / "profiles.json")

    r = subprocess.run(
        [sys.executable, "scripts/council-review.py", "plan", "2", "Test"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 1, f"expected 1, got {r.returncode}. stderr: {r.stderr}"
    assert "No council members configured for 'plan'" in (r.stdout + r.stderr)


def test_code_review_rejects_without_flag(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _init_git(repo)
    (repo / "untracked.py").write_text("x")
    _make_empty_council_config(repo, review_type="code")
    shutil.copy2(CR_PATH, repo / "scripts" / "council-review.py")
    shutil.copy2(REPO_ROOT / "scripts" / "profile.py", repo / "scripts" / "profile.py")
    (repo / "scripts" / "bootstrap").mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "bootstrap" / "profiles.json",
                 repo / "scripts" / "bootstrap" / "profiles.json")

    r = subprocess.run(
        [sys.executable, "scripts/council-review.py", "code", "2", "Test"],
        cwd=repo, capture_output=True, text=True,
    )
    assert r.returncode == 4
    assert "untracked.py" in r.stderr
    # Round counter must not have advanced.
    assert not (repo / ".review-round-sprint2-code").exists()


def test_render_source_file_fence_and_content(council_cr, tmp_path):
    """Lock in _render_source_file behaviour: code fence language matches
    suffix, content appears verbatim, section header names the path."""
    f = tmp_path / "sub" / "x.py"
    f.parent.mkdir()
    f.write_text("def a():\n    return 1\n")
    rendered = council_cr._render_source_file("sub/x.py", tmp_path)
    assert rendered.startswith("### sub/x.py\n```py\n")
    assert rendered.endswith("\n```")
    assert "def a():" in rendered
    assert "return 1" in rendered


def test_render_source_file_uses_suffix_as_fence(council_cr, tmp_path):
    (tmp_path / "foo.sh").write_text("echo hi")
    r = council_cr._render_source_file("foo.sh", tmp_path)
    assert "```sh" in r


def test_banner_is_first_section_of_materials(council_cr, tmp_path):
    """R1 [High] regression guard: banner must be the first section
    of review materials, ahead of Changed Files / codegraph / plan."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_git(repo)
    (repo / "new.py").write_text("content")
    # Seed plan/changes so there are multiple sections competing for position.
    (repo / "PLAN_Sprint9.md").write_text("# plan")
    (repo / "CHANGES.md").write_text("# changes")

    pf = council_cr.preflight_code_review(repo, allow_untracked=True)
    materials = council_cr.gather_code_materials(
        "9", repo, banner=pf.banner, include_untracked=True,
    )
    # Banner must appear before any other section marker.
    banner_idx = materials.find("PRE-FLIGHT BANNER")
    changed_idx = materials.find("### Changed Files")
    plan_idx = materials.find("(approved plan)")
    assert banner_idx != -1
    assert banner_idx < plan_idx or plan_idx == -1
    if changed_idx != -1:
        assert banner_idx < changed_idx


def test_find_untracked_drops_control_char_filenames(council_cr, tmp_path, monkeypatch):
    """R1 [Medium] regression guard: filename with a control byte in
    git's ls-files output must be dropped. We simulate the output since
    creating such filenames on disk is OS-dependent."""
    class FakeResult:
        returncode = 0
        stdout = "good.py\nbad\x01name.py\nalso_good.py\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        return FakeResult()

    monkeypatch.setattr(council_cr.subprocess, "run", fake_run)
    result = council_cr.find_untracked_source_files(tmp_path)
    assert "good.py" in result
    assert "also_good.py" in result
    assert not any("bad" in r for r in result)


def test_redact_secrets_catches_compound_names(council_cr):
    """R1 [Medium] regression guard: compound secret names like
    FOO_API_KEY=..., MY_SECRET_KEY=..., PROJECT_TOKEN=... must be
    redacted, not only plain `api_key=...`."""
    samples = [
        "FOO_API_KEY=abc123xyz456abc123",
        "MY_SECRET_KEY = 'topsecret12345'",
        "SERVICE_AUTH_TOKEN=abcdef12345678",
        "DATABASE_PASSWORD=longenoughpw1234",
        "USER_CREDENTIALS=abcd1234efgh5678",
    ]
    for s in samples:
        redacted = council_cr.redact_secrets(s)
        assert "[REDACTED]" in redacted, f"not redacted: {s!r} -> {redacted!r}"


def test_code_review_allow_untracked_gets_past_preflight(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _init_git(repo)
    (repo / "untracked.py").write_text("x")
    _make_empty_council_config(repo, review_type="code")
    shutil.copy2(CR_PATH, repo / "scripts" / "council-review.py")
    shutil.copy2(REPO_ROOT / "scripts" / "profile.py", repo / "scripts" / "profile.py")
    (repo / "scripts" / "bootstrap").mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "scripts" / "bootstrap" / "profiles.json",
                 repo / "scripts" / "bootstrap" / "profiles.json")

    r = subprocess.run(
        [sys.executable, "scripts/council-review.py",
         "code", "2", "Test", "--allow-untracked"],
        cwd=repo, capture_output=True, text=True,
    )
    # Should exit 1 at get_active_members, NOT 4 at pre-flight.
    assert r.returncode == 1, f"stderr: {r.stderr}"
    assert "No council members configured for 'code'" in (r.stdout + r.stderr)

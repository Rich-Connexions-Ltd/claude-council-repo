"""File: tests/test_bootstrap.py

Purpose: Per-step coverage of the interactive wizard in scripts/bootstrap.py.
Exercises identity/stack/knowledge/sprints/council + profile application
under mocked input() / subprocess.run, and asserts the answers-file
contract (missing keys must raise AnswersFileKeyMissing rather than
falling back to input()).

Role:
  Fills the gap between test_bootstrap_security.py (prompt-injection /
  permission surface) and test_template_bootstrap.py (end-to-end parity).
  These tests run in well under a second; the wizard steps are
  exercised directly without shelling out to a bootstrap subprocess.

Invariants & gotchas:
  - Each test that writes to REPO_ROOT must rebind bs.REPO_ROOT (and
    the module-level constants derived from it) to a tmp_path so the
    real repo is untouched.
  - _ANSWERS is a module global; tests set it via monkeypatch so state
    doesn't leak between tests.

Last updated: Sprint 7 (2026-04-16) -- step6 library-selection path tests (library pick, skip, generate fallback).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bs(bootstrap_module):
    return bootstrap_module


@pytest.fixture
def tmp_repo(tmp_path, bs, monkeypatch):
    """Rebind bootstrap's REPO_ROOT + PROMPTS_DIR + BOOTSTRAP_MARKER to
    an isolated tmp_path so tests don't mutate the real checkout."""
    (tmp_path / "scripts" / "bootstrap").mkdir(parents=True)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)
    (tmp_path / "knowledge").mkdir(parents=True)
    (tmp_path / "memory").mkdir(parents=True)
    (tmp_path / ".claude").mkdir(parents=True)

    # Copy the prompt files bootstrap reads from PROMPTS_DIR — enough
    # for the step functions we exercise here.
    for rel in [
        "scripts/bootstrap/generate-indexer-prompt.md",
        "scripts/bootstrap/summarize-knowledge-prompt.md",
        "scripts/bootstrap/domain-expert-prompt.md",
        "scripts/bootstrap/profiles.json",
        "scripts/bootstrap/component_files.json",
        "scripts/bootstrap/settings.template.json",
        "scripts/profile.py",
    ]:
        src = REPO_ROOT / rel
        if src.exists():
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Seed a couple of stub indexer files so _prune_unused_indexers has
    # something to prune.
    for lang in ("python", "typescript", "go", "rust", "java"):
        stub = tmp_path / "scripts" / "indexers" / f"{lang}.py"
        stub.write_text(f'"""stub indexer for {lang}"""\n')
    (tmp_path / "scripts" / "indexers" / "__init__.py").write_text("")

    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bs, "BOOTSTRAP_MARKER", tmp_path / ".bootstrap-complete")
    monkeypatch.setattr(bs, "PROMPTS_DIR", tmp_path / "scripts" / "bootstrap")
    return tmp_path


@pytest.fixture
def set_answers(bs, monkeypatch):
    """Install an answers dict as the _ANSWERS global for this test."""

    def _set(answers: dict) -> None:
        monkeypatch.setattr(bs, "_ANSWERS", answers)

    return _set


# ---------------------------------------------------------------------------
# Step 1 — identity
# ---------------------------------------------------------------------------


def test_step1_identity_populates_context(bs, tmp_repo, set_answers):
    set_answers({
        "identity.project_name": "demo",
        "identity.mvp_outcome": "ship demo so users can try it",
        "identity.has_brief": False,
    })
    ctx: dict = {}
    bs.step1_identity(ctx)
    assert ctx["project_name"] == "demo"
    assert ctx["mvp_outcome"].startswith("ship demo")
    assert ctx["brief"] == ""


def test_load_answers_file_rejects_fence_token(
    bs, tmp_path, monkeypatch
):
    """Fence tokens in the answers file must be rejected at load time,
    before any step function sees the value."""
    monkeypatch.chdir(tmp_path)
    payload = {
        "identity.project_name": f"evil {bs.FENCE_BEGIN} hijack",
        "identity.mvp_outcome": "ship",
    }
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps(payload))
    with pytest.raises(SystemExit):
        bs._load_answers_file(str(answers_file))


# ---------------------------------------------------------------------------
# Step 2 — stack
# ---------------------------------------------------------------------------


def test_step2_stack_python_only(bs, tmp_repo, set_answers):
    set_answers({
        "stack.languages": ["python"],
        "stack.framework.python": "none",
    })
    ctx: dict = {}
    bs.step2_stack(ctx)
    assert ctx["languages"] == ["python"]
    assert ctx["frameworks"] == {"python": "none"}
    assert ctx["other_languages"] == []
    # Non-python canned indexers were pruned; python.py is always kept.
    remaining = {
        p.name for p in (tmp_repo / "scripts" / "indexers").iterdir()
        if p.is_file()
    }
    assert "python.py" in remaining
    assert "typescript.py" not in remaining


def test_step2_stack_typescript_with_framework(bs, tmp_repo, set_answers):
    set_answers({
        "stack.languages": ["typescript"],
        "stack.framework.typescript": "Next.js",
    })
    ctx: dict = {}
    bs.step2_stack(ctx)
    assert ctx["frameworks"] == {"typescript": "Next.js"}
    remaining = {
        p.name for p in (tmp_repo / "scripts" / "indexers").iterdir()
        if p.is_file()
    }
    # typescript.py kept because it's selected; python.py kept as
    # always-on scaffold.
    assert "typescript.py" in remaining
    assert "python.py" in remaining
    assert "go.py" not in remaining


def test_step2_stack_other_language_captured(bs, tmp_repo, set_answers):
    set_answers({
        "stack.languages": ["other..."],
        "stack.other_language": "kotlin",
    })
    ctx: dict = {}
    bs.step2_stack(ctx)
    assert ctx["languages"] == []
    assert ctx["other_languages"] == ["kotlin"]


# ---------------------------------------------------------------------------
# Step 2b — other-language generator
# ---------------------------------------------------------------------------


def test_step2b_skipped_when_no_other_languages(bs, tmp_repo, set_answers):
    ctx: dict = {
        "languages": ["python"], "frameworks": {}, "other_languages": []
    }
    # Must not raise or touch anything.
    bs.step2b_generate_other(ctx)


def test_step2b_skips_generator_when_user_declines(
    bs, tmp_repo, set_answers, monkeypatch
):
    set_answers({
        "stack.other.extension.kotlin": "kt",
        "stack.other.grammar.kotlin": "kotlin",
        "stack.other.invoke_generator.kotlin": False,
    })
    called: dict = {}

    def fake_run_claude(prompt, *, allow_edits=False, **kwargs):
        called["yes"] = True
        return (False, "", "should not be called")

    monkeypatch.setattr(bs, "run_claude_cli", fake_run_claude)
    ctx = {
        "languages": ["python"], "frameworks": {},
        "other_languages": ["kotlin"],
    }
    bs.step2b_generate_other(ctx)
    assert "yes" not in called, (
        "run_claude_cli must not run when user declines invocation"
    )
    assert (tmp_repo / "scripts" / "bootstrap" / "_generated_kotlin.md").exists()


# ---------------------------------------------------------------------------
# Step 3 — knowledge base
# ---------------------------------------------------------------------------


def test_step3_short_circuits_when_no_files_flag(bs, tmp_repo, set_answers):
    set_answers({"knowledge.has_files": False})
    ctx: dict = {}
    bs.step3_knowledge(ctx)
    assert ctx["knowledge_seeded"] is False


def test_step3_short_circuits_when_raw_empty(bs, tmp_repo, set_answers):
    set_answers({
        "knowledge.has_files": True,
    })
    # raw dir exists (step3 creates it) but contains no files.
    ctx: dict = {}
    bs.step3_knowledge(ctx)
    assert ctx["knowledge_seeded"] is False


# ---------------------------------------------------------------------------
# Step 4 — sprints
# ---------------------------------------------------------------------------


def test_step4_skip_mode_leaves_sprints_md_untouched(
    bs, tmp_repo, set_answers
):
    set_answers({"sprints.mode": "Skip — I'll do this later"})
    (tmp_repo / "SPRINTS.md").write_text("# pre-existing\n")
    bs.step4_sprints({"mvp_outcome": "x", "brief": "", "project_name": "p"})
    assert (tmp_repo / "SPRINTS.md").read_text() == "# pre-existing\n"


# ---------------------------------------------------------------------------
# Step 5 — council toggle
# ---------------------------------------------------------------------------


def test_step5_skip_disables_council(bs, tmp_repo, set_answers):
    set_answers({
        "council.review_mode":
            "Skip council entirely — ship as solo dev without reviewer"
    })
    ctx: dict = {}
    bs.step5_council(ctx)
    assert ctx["council_enabled"] is False


def test_step5_auto_writes_human_review_off(bs, tmp_repo, set_answers):
    set_answers({
        "council.review_mode":
            "Fully automated (Claude acts on verdicts immediately)"
    })
    ctx: dict = {}
    bs.step5_council(ctx)
    assert ctx["council_enabled"] is True
    assert (tmp_repo / "memory" / "human-review-mode").read_text() == "off\n"


def test_step5_human_writes_human_review_on(bs, tmp_repo, set_answers):
    set_answers({
        "council.review_mode":
            "Human-in-loop (you approve each verdict)"
    })
    ctx: dict = {}
    bs.step5_council(ctx)
    assert ctx["council_enabled"] is True
    assert (tmp_repo / "memory" / "human-review-mode").read_text() == "on\n"


# ---------------------------------------------------------------------------
# Profile application
# ---------------------------------------------------------------------------


def test_apply_profile_minimal_disables_council_components(
    bootstrap_minimal,
):
    settings = (bootstrap_minimal / ".claude" / "settings.json").read_text()
    settings_obj = json.loads(settings)
    # The minimal profile must not enable council-review components.
    for entry in settings_obj.get("hooks", {}).values():
        for hook in entry:
            matcher = hook.get("matcher", "")
            assert "council" not in matcher.lower()
    # Council scripts must have been removed.
    assert not (bootstrap_minimal / "scripts" / "council-review.py").exists()


def test_apply_profile_standard_keeps_council(bootstrap_standard):
    assert (bootstrap_standard / "scripts" / "council-review.py").exists()
    assert (bootstrap_standard / ".claude" / "settings.json").exists()


def test_apply_profile_full_enables_digest(bootstrap_full):
    assert (bootstrap_full / "scripts" / "findings-digest.py").exists()


# ---------------------------------------------------------------------------
# --answers-file contract
# ---------------------------------------------------------------------------


def test_answered_returns_false_when_no_answers_file(bs, monkeypatch):
    monkeypatch.setattr(bs, "_ANSWERS", None)
    present, value = bs._answered("some.key")
    assert present is False and value is None


def test_answered_raises_when_key_missing(bs, set_answers):
    set_answers({"some.other.key": "x"})
    with pytest.raises(bs.AnswersFileKeyMissing):
        bs._answered("not-present")


def test_answered_returns_value_when_present(bs, set_answers):
    set_answers({"identity.project_name": "hi"})
    present, value = bs._answered("identity.project_name")
    assert present is True and value == "hi"


def test_ask_wrong_type_raises(bs, set_answers):
    """ask() expects a string from the answers-file; a non-string must
    raise AnswersFileKeyMissing rather than silently stringifying."""
    set_answers({"identity.project_name": 42})
    with pytest.raises(bs.AnswersFileKeyMissing):
        bs.ask("x", prompt_id="identity.project_name")


# ---------------------------------------------------------------------------
# Sprint 7: step6 library-selection path
# ---------------------------------------------------------------------------


def test_step6_library_skip_when_council_disabled(bs, tmp_repo, set_answers):
    """Step 6 is a no-op when council_enabled is False."""
    ctx = {"council_enabled": False}
    bs.step6_domain_expert(ctx)
    # Should not have touched anything; no exceptions.


def _stage_council_config(tmp_repo):
    """Helper: seed a minimal council-config.json the step6 library
    path can mutate."""
    cfg = tmp_repo / "scripts" / "council-config.json"
    cfg.parent.mkdir(exist_ok=True)
    import json as _json
    cfg.write_text(_json.dumps({
        "council": {
            "members": [
                {"role": "domain", "lens": "PLACEHOLDER"},
                {"role": "security", "lens": "sec lens"},
            ]
        }
    }, indent=2))
    return cfg


def _stage_library(tmp_repo, bs, monkeypatch):
    """Copy the real library into tmp_repo/scripts/bootstrap/domain-experts/
    and point PROMPTS_DIR at it. The tmp_repo fixture already
    monkeypatched PROMPTS_DIR; we re-stage from REPO_ROOT (the actual
    checkout) so the source lookup succeeds."""
    import shutil as _shutil
    src = REPO_ROOT / "scripts" / "bootstrap" / "domain-experts"
    dst = tmp_repo / "scripts" / "bootstrap" / "domain-experts"
    if dst.exists():
        _shutil.rmtree(dst)
    _shutil.copytree(src, dst)
    monkeypatch.setattr(bs, "PROMPTS_DIR", tmp_repo / "scripts" / "bootstrap")


def test_step6_library_pick_writes_lens(
    bs, tmp_repo, set_answers, monkeypatch
):
    """Pick a library entry and verify council-config.json's domain
    member gets the picked lens."""
    _stage_library(tmp_repo, bs, monkeypatch)
    cfg = _stage_council_config(tmp_repo)
    library = bs._load_domain_expert_library()
    # Compose the exact option string step6 renders.
    entry = next(e for e in library if e["slug"] == "backend-python")
    choice_str = f"Library: {entry['name']} — {entry['summary']}"
    set_answers({"council.domain_expert_choice": choice_str})
    bs.step6_domain_expert({"council_enabled": True, "languages": ["python"]})
    import json as _json
    loaded = _json.loads(cfg.read_text())
    domain = next(
        m for m in loaded["council"]["members"] if m["role"] == "domain"
    )
    assert "PLACEHOLDER" not in domain["lens"]
    assert len(domain["lens"].split()) >= 50


def test_step6_skip_leaves_config_untouched(
    bs, tmp_repo, set_answers, monkeypatch
):
    _stage_library(tmp_repo, bs, monkeypatch)
    cfg = _stage_council_config(tmp_repo)
    original = cfg.read_text()
    set_answers({
        "council.domain_expert_choice": "Skip — no domain expert for this project",
    })
    bs.step6_domain_expert({"council_enabled": True, "languages": ["python"]})
    assert cfg.read_text() == original


def test_step6_generate_path_invokes_claude_cli(
    bs, tmp_repo, set_answers, monkeypatch
):
    """The 'Generate' branch still runs run_claude_cli. We mock it so
    no subprocess fires; knowledge/ needs >=2 files to pass the
    'too thin' gate."""
    _stage_library(tmp_repo, bs, monkeypatch)
    _stage_council_config(tmp_repo)
    k = tmp_repo / "knowledge"
    k.mkdir(exist_ok=True)
    (k / "arch.md").write_text("# arch\n")
    (k / "domain.md").write_text("# domain\n")
    # Copy domain-expert-prompt.md into the staged prompts dir.
    import shutil as _shutil
    _shutil.copy2(
        REPO_ROOT / "scripts" / "bootstrap" / "domain-expert-prompt.md",
        tmp_repo / "scripts" / "bootstrap" / "domain-expert-prompt.md",
    )
    called: dict = {}

    def fake_run(prompt, *, allow_edits=False, **kwargs):
        called["invoked"] = True
        called["allow_edits"] = allow_edits
        return (True, "ok", "")

    monkeypatch.setattr(bs, "run_claude_cli", fake_run)
    set_answers({
        "council.domain_expert_choice":
            "Generate a custom lens from knowledge/ (uses claude CLI)",
    })
    bs.step6_domain_expert({"council_enabled": True, "languages": ["python"]})
    assert called.get("invoked") is True
    assert called.get("allow_edits") is True

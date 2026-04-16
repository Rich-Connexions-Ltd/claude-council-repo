"""File: tests/test_domain_experts.py

Purpose: Sprint 7 library tests. Asserts every entry in
scripts/bootstrap/domain-experts/ has valid YAML-ish frontmatter
plus the four mandatory sections, and that the slug matches the
filename stem. Also covers the bootstrap library-selection and
scaffold-seed paths.

Last updated: Sprint 7 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = REPO_ROOT / "scripts" / "bootstrap" / "domain-experts"
SCAFFOLD_SUBDIRS = ("architecture", "domain", "runbook", "decisions")
REQUIRED_SECTIONS = (
    "## Lens description",
    "## Domain invariants",
    "## Finding heuristics",
    "## Anti-scope",
)
REQUIRED_META_KEYS = {"name", "slug", "stacks", "summary"}


@pytest.fixture
def library_files() -> list[Path]:
    assert LIBRARY_DIR.is_dir(), "domain-experts library dir is missing"
    files = sorted(LIBRARY_DIR.glob("*.md"))
    assert files, "library dir has no entries"
    return files


# ---------------------------------------------------------------------------
# Library entry structural tests
# ---------------------------------------------------------------------------


def test_library_has_at_least_five_entries(library_files):
    assert len(library_files) >= 5, (
        f"Sprint 7 exit criterion: 5 domain-expert entries ship. "
        f"Found: {[f.name for f in library_files]}"
    )


def test_every_entry_has_required_frontmatter(library_files, bootstrap_module):
    for path in library_files:
        meta = bootstrap_module._parse_library_frontmatter(path)
        assert meta is not None, f"{path.name}: frontmatter parse failed"
        assert REQUIRED_META_KEYS.issubset(meta.keys()), (
            f"{path.name}: missing frontmatter keys "
            f"{REQUIRED_META_KEYS - meta.keys()}"
        )
        assert isinstance(meta["stacks"], list), (
            f"{path.name}: stacks must be a list (got {type(meta['stacks'])})"
        )
        assert meta["stacks"], f"{path.name}: stacks list is empty"


def test_slug_matches_filename_stem(library_files, bootstrap_module):
    for path in library_files:
        meta = bootstrap_module._parse_library_frontmatter(path)
        assert meta["slug"] == path.stem, (
            f"{path.name}: slug {meta['slug']!r} != stem {path.stem!r}"
        )


def test_every_entry_has_required_sections(library_files):
    for path in library_files:
        text = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            assert section in text, (
                f"{path.name}: missing required section {section!r}"
            )


def test_every_entry_has_extractable_lens(library_files, bootstrap_module):
    for path in library_files:
        lens = bootstrap_module._extract_lens_description(path)
        assert lens, f"{path.name}: lens extraction returned empty"
        # A useful lens is at least 50 words.
        assert len(lens.split()) >= 50, (
            f"{path.name}: lens text is suspiciously short "
            f"({len(lens.split())} words)"
        )


def test_no_fence_token_in_lens(library_files, bootstrap_module):
    """Fence tokens would collide with bootstrap's prompt composition."""
    for path in library_files:
        lens = bootstrap_module._extract_lens_description(path)
        assert bootstrap_module.FENCE_BEGIN not in lens, (
            f"{path.name}: lens contains FENCE_BEGIN"
        )
        assert bootstrap_module.FENCE_END not in lens, (
            f"{path.name}: lens contains FENCE_END"
        )


def test_expected_stack_coverage(library_files, bootstrap_module):
    """Sprint 7 ships entries for these five stacks."""
    slugs = set()
    for path in library_files:
        meta = bootstrap_module._parse_library_frontmatter(path)
        slugs.add(meta["slug"])
    expected = {
        "web-typescript-react", "backend-python", "backend-go",
        "rust-systems", "data-pipeline",
    }
    assert expected <= slugs, f"missing library entries: {expected - slugs}"


# ---------------------------------------------------------------------------
# Bootstrap library-selection helpers
# ---------------------------------------------------------------------------


def test_load_domain_expert_library_returns_entries(bootstrap_module):
    entries = bootstrap_module._load_domain_expert_library()
    assert len(entries) >= 5
    for entry in entries:
        for key in ("name", "slug", "stacks", "summary", "path"):
            assert key in entry, f"missing {key!r} in {entry!r}"


def test_recommend_library_entry_matches_on_stack(bootstrap_module):
    library = bootstrap_module._load_domain_expert_library()
    ctx = {"languages": ["python"], "other_languages": []}
    rec = bootstrap_module._recommend_library_entry(library, ctx)
    assert rec is not None
    assert "python" in rec["stacks"]


def test_recommend_library_entry_returns_none_when_no_match(bootstrap_module):
    library = bootstrap_module._load_domain_expert_library()
    ctx = {"languages": ["cobol"], "other_languages": []}
    rec = bootstrap_module._recommend_library_entry(library, ctx)
    assert rec is None


def test_recommend_library_entry_no_stacks_returns_none(bootstrap_module):
    library = bootstrap_module._load_domain_expert_library()
    rec = bootstrap_module._recommend_library_entry(library, {})
    assert rec is None


def test_apply_library_lens_writes_council_config(
    bootstrap_module, tmp_path, monkeypatch
):
    """End-to-end: picking a library entry rewrites the domain
    member's `lens` field in council-config.json."""
    # Stage a minimal project root with a council-config.json.
    monkeypatch.setattr(bootstrap_module, "REPO_ROOT", tmp_path)
    cfg_path = tmp_path / "scripts" / "council-config.json"
    cfg_path.parent.mkdir()
    cfg_path.write_text(json.dumps({
        "council": {
            "members": [
                {"role": "domain", "lens": "PLACEHOLDER"},
                {"role": "security", "lens": "sec lens"},
            ]
        }
    }, indent=2))
    # Pick any library entry.
    library = bootstrap_module._load_domain_expert_library()
    entry = library[0]
    ok, message = bootstrap_module._apply_library_lens(entry)
    assert ok, message
    loaded = json.loads(cfg_path.read_text())
    domain = next(
        m for m in loaded["council"]["members"] if m["role"] == "domain"
    )
    assert "PLACEHOLDER" not in domain["lens"]
    assert len(domain["lens"]) > 50


def test_apply_library_lens_missing_config_fails_cleanly(
    bootstrap_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(bootstrap_module, "REPO_ROOT", tmp_path)
    library = bootstrap_module._load_domain_expert_library()
    ok, message = bootstrap_module._apply_library_lens(library[0])
    assert ok is False
    assert "council-config.json" in message


def test_apply_library_lens_missing_domain_role_fails(
    bootstrap_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(bootstrap_module, "REPO_ROOT", tmp_path)
    cfg_path = tmp_path / "scripts" / "council-config.json"
    cfg_path.parent.mkdir()
    cfg_path.write_text(json.dumps({
        "council": {"members": [{"role": "security", "lens": "x"}]}
    }))
    library = bootstrap_module._load_domain_expert_library()
    ok, message = bootstrap_module._apply_library_lens(library[0])
    assert ok is False
    assert "domain" in message


# ---------------------------------------------------------------------------
# Knowledge scaffold
# ---------------------------------------------------------------------------


def test_scaffold_subdirs_exist_in_dev_container():
    """The scaffold README templates ship from the dev-container so
    they're available to the template sync and every bootstrapped
    project."""
    k = REPO_ROOT / "knowledge"
    for sub in SCAFFOLD_SUBDIRS:
        readme = k / sub / "README.md"
        assert readme.is_file(), f"missing scaffold README: {readme}"


def test_scaffold_readmes_have_purpose_and_cross_links():
    """Every scaffold README opens with the "What lives here / does
    not / See also" preamble so new users learn the separation."""
    k = REPO_ROOT / "knowledge"
    for sub in SCAFFOLD_SUBDIRS:
        text = (k / sub / "README.md").read_text(encoding="utf-8")
        assert "*What lives here:*" in text, f"{sub}: missing 'What lives here'"
        assert "*What does not:*" in text, f"{sub}: missing 'What does not'"
        assert "*See also:*" in text, f"{sub}: missing 'See also'"


def test_scaffold_is_present_helper(bootstrap_module):
    """The helper in bootstrap that gates step3's summariser behaviour
    on the scaffold's presence returns True for the dev-container."""
    assert bootstrap_module._scaffold_is_present() is True


def test_scaffold_is_present_helper_false_for_empty(
    bootstrap_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(bootstrap_module, "REPO_ROOT", tmp_path)
    assert bootstrap_module._scaffold_is_present() is False


# ---------------------------------------------------------------------------
# Classifier meta-prompt structural test
# ---------------------------------------------------------------------------


def test_classifier_meta_prompt_exists_and_well_formed():
    path = REPO_ROOT / "scripts" / "bootstrap" / "classify-knowledge-prompt.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    # Must have the four target subdir names so the model knows the
    # enum.
    for sub in SCAFFOLD_SUBDIRS:
        assert f"**{sub}**" in text, f"classifier missing '{sub}' description"
    # Must state a JSON output schema.
    assert '"target_subdir"' in text
    assert '"confidence"' in text
    assert '"reason"' in text
    # Must have fence tokens so user-supplied content gets treated as data.
    assert "<<<USER_INPUT_BEGIN>>>" in text
    assert "<<<USER_INPUT_END>>>" in text

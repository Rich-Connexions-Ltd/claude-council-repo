"""File: tests/test_skills.py

Purpose: Smoke coverage for skill files — existence per profile, YAML
frontmatter validity (via safe_load), non-empty name/description fields.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

EXPECTED_SKILLS = [
    "sprint-start.md",
    "sprint-complete.md",
    "check-headers.md",
    "council-plan.md",
    "council-code.md",
]


def _parse_frontmatter(text: str) -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover - environment without pyyaml
        pytest.skip("pyyaml not installed")
    assert text.startswith("---"), "skill file must start with YAML frontmatter"
    end = text.index("\n---", 3)
    fm = text[3:end].strip()
    return yaml.safe_load(fm)


def test_all_skill_files_exist_in_template():
    for name in EXPECTED_SKILLS:
        assert (SKILLS_DIR / name).exists(), f"missing skill: {name}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_skill_frontmatter_valid(name):
    data = _parse_frontmatter((SKILLS_DIR / name).read_text())
    assert isinstance(data, dict)
    assert data.get("name"), f"{name}: empty 'name'"
    assert data.get("description"), f"{name}: empty 'description'"
    assert data["name"] == name.removesuffix(".md")


def test_safe_yaml_rejects_unsafe_tags(tmp_path):
    try:
        import yaml
    except ImportError:
        pytest.skip("pyyaml not installed")
    unsafe = tmp_path / "evil.md"
    unsafe.write_text(
        "---\nname: evil\ndescription: x\npayload: !!python/object/apply:os.system ['echo pwned']\n---\nbody\n"
    )
    text = unsafe.read_text()
    end = text.index("\n---", 3)
    fm = text[3:end].strip()
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(fm)


def test_minimal_excludes_all_skills(bootstrap_minimal):
    skills = bootstrap_minimal / ".claude" / "skills"
    if skills.exists():
        assert not any(skills.glob("*.md"))


def test_standard_includes_all_skills(bootstrap_standard):
    skills = bootstrap_standard / ".claude" / "skills"
    for name in EXPECTED_SKILLS:
        assert (skills / name).exists(), f"standard missing: {name}"

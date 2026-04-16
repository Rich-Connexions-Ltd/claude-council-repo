"""File: tests/test_tag_derivation.py

Purpose: Deterministic tag derivation from finding titles.

Last updated: Sprint 1 (2026-04-15) -- initial coverage
"""

from __future__ import annotations


def test_basic(council_review_module):
    assert council_review_module._derive_tag(
        "Findings digest is structurally infeasible"
    ) == "findings-digest-structurally-inf"


def test_stopwords_dropped(council_review_module):
    assert council_review_module._derive_tag(
        "The plan of the ages is upon us"
    ) == "plan-ages-upon-us"


def test_empty(council_review_module):
    assert council_review_module._derive_tag("") == "untagged"
    assert council_review_module._derive_tag("   ") == "untagged"


def test_punctuation_stripped(council_review_module):
    assert council_review_module._derive_tag(
        "Tag derivation: deterministic rule!"
    ).startswith("tag-derivation")


def test_unicode_normalised(council_review_module):
    # NFKD normalisation + ascii fold
    tag = council_review_module._derive_tag("Café naïve façade")
    assert "cafe" in tag or tag == "cafe-naive-facade"


def test_length_cap(council_review_module):
    long = "a" * 100 + " " + "b" * 100
    out = council_review_module._derive_tag(long)
    assert len(out) <= 32


def test_deterministic(council_review_module):
    title = "Profile handling lacks a single authoritative definition"
    runs = {council_review_module._derive_tag(title) for _ in range(20)}
    assert len(runs) == 1


def test_only_stopwords_yields_untagged(council_review_module):
    assert council_review_module._derive_tag("the of the and or") == "untagged"


def test_lens_from_source_annotation(council_review_module):
    assert council_review_module._derive_lens(
        "- **[High]** Example (Source: security)"
    ) == "security"
    assert council_review_module._derive_lens(
        "- **[High]** Example (Source: code_quality, domain)"
    ) == "code_quality"
    assert council_review_module._derive_lens(
        "- **[High]** Example (Source: Test Quality Expert)"
    ) == "test_quality"


def test_lens_missing_annotation(council_review_module):
    assert council_review_module._derive_lens("- **[High]** no source") == "unknown"

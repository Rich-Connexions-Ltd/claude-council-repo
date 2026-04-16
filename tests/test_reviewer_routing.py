"""File: tests/test_reviewer_routing.py

Purpose: Coverage for Sprint 6 selective reviewer routing —
--lenses / --auto-lenses / --allow-no-security parsing, security
non-removable rule, degenerate inputs, tracker v3 migration, and
multi-round skipped-lens invariants.

Last updated: Sprint 6 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cr(council_review_module):
    return council_review_module


@pytest.fixture
def fake_config():
    return {
        "council": {
            "members": [
                {"role": "security", "label": "Security", "phases": ["plan", "code"]},
                {"role": "code_quality", "label": "Code Quality", "phases": ["plan", "code"]},
                {"role": "test_quality", "label": "Test Quality", "phases": ["plan", "code"]},
                {"role": "domain", "label": "Domain", "phases": ["plan", "code"]},
            ]
        }
    }


# ---------------------------------------------------------------------------
# parse_lenses_arg — degenerate inputs (R1 #9)
# ---------------------------------------------------------------------------


def test_parse_lenses_basic(cr):
    result = cr.parse_lenses_arg(
        "security,code_quality",
        ["security", "code_quality", "test_quality", "domain"],
    )
    assert result == {"security", "code_quality"}


def test_parse_lenses_rejects_empty(cr):
    with pytest.raises(cr.LensArgError, match="empty"):
        cr.parse_lenses_arg("", ["security", "code_quality"])


def test_parse_lenses_rejects_whitespace_only(cr):
    with pytest.raises(cr.LensArgError, match="empty"):
        cr.parse_lenses_arg("   ", ["security"])


def test_parse_lenses_rejects_lone_comma(cr):
    with pytest.raises(cr.LensArgError, match="lone comma"):
        cr.parse_lenses_arg(",", ["security"])


def test_parse_lenses_rejects_embedded_blank(cr):
    with pytest.raises(cr.LensArgError, match="empty entry"):
        cr.parse_lenses_arg("security,,code_quality", ["security", "code_quality"])


def test_parse_lenses_rejects_duplicates(cr):
    with pytest.raises(cr.LensArgError, match="duplicate"):
        cr.parse_lenses_arg("security,security", ["security"])


def test_parse_lenses_rejects_unknown(cr):
    with pytest.raises(cr.LensArgError, match="unknown"):
        cr.parse_lenses_arg("security,unknown_lens", ["security", "code_quality"])


def test_parse_lenses_rejects_single_unknown_in_list(cr):
    """A list of 3 with one unknown fails as a group — don't silently drop."""
    with pytest.raises(cr.LensArgError, match="unknown"):
        cr.parse_lenses_arg(
            "security,code_quality,typo",
            ["security", "code_quality", "test_quality"],
        )


def test_parse_lenses_strips_whitespace(cr):
    result = cr.parse_lenses_arg(
        " security , code_quality ",
        ["security", "code_quality"],
    )
    assert result == {"security", "code_quality"}


# ---------------------------------------------------------------------------
# auto_lens_set
# ---------------------------------------------------------------------------


def test_auto_lens_tests_only_diff(cr):
    """R1: tests-only diff routes to test_quality + security +
    code_quality (3 members; code_quality is always-on)."""
    result = cr.auto_lens_set(
        ["tests/test_foo.py"],
        ["security", "code_quality", "test_quality", "domain"],
    )
    assert result == {"security", "code_quality", "test_quality"}


def test_auto_lens_knowledge_only_diff(cr):
    result = cr.auto_lens_set(
        ["knowledge/architecture.md"],
        ["security", "code_quality", "test_quality", "domain"],
    )
    assert result == {"security", "code_quality", "domain"}


def test_auto_lens_mixed_diff(cr):
    result = cr.auto_lens_set(
        ["tests/test_foo.py", "knowledge/x.md", "scripts/bar.py"],
        ["security", "code_quality", "test_quality", "domain"],
    )
    assert result == {"security", "code_quality", "test_quality", "domain"}


def test_auto_lens_empty_diff(cr):
    result = cr.auto_lens_set(
        [], ["security", "code_quality", "test_quality", "domain"],
    )
    assert result == {"security", "code_quality"}


def test_auto_lens_drops_unconfigured_roles(cr):
    """If the council config omits a role (no test_quality member),
    the auto-lens set drops it silently."""
    result = cr.auto_lens_set(
        ["tests/test_foo.py"],
        ["security", "code_quality"],
    )
    assert result == {"security", "code_quality"}


# ---------------------------------------------------------------------------
# enforce_security_lens — R1 #5
# ---------------------------------------------------------------------------


def test_enforce_security_fails_closed_on_code(cr):
    with pytest.raises(cr.LensArgError, match="security lens"):
        cr.enforce_security_lens(
            {"code_quality", "domain"},
            allow_no_security=False,
            review_type="code",
        )


def test_enforce_security_allows_with_override(cr):
    lenses, bypassed = cr.enforce_security_lens(
        {"code_quality", "domain"},
        allow_no_security=True,
        review_type="code",
    )
    assert lenses == {"code_quality", "domain"}
    assert bypassed is True


def test_enforce_security_no_op_when_present(cr):
    lenses, bypassed = cr.enforce_security_lens(
        {"security", "code_quality"},
        allow_no_security=False,
        review_type="code",
    )
    assert lenses == {"security", "code_quality"}
    assert bypassed is False


def test_enforce_security_empty_set_ok_on_plan(cr):
    """Plan reviews with empty lenses (the CLI default) pass through."""
    lenses, bypassed = cr.enforce_security_lens(
        set(),
        allow_no_security=False,
        review_type="plan",
    )
    assert lenses == set()
    assert bypassed is False


def test_enforce_security_rejects_lenses_on_plan(cr):
    """Sprint 6 R1 #17: any non-empty lens set on a plan review is
    rejected at the shared-logic layer, not just at argparse."""
    with pytest.raises(cr.LensArgError, match="plan"):
        cr.enforce_security_lens(
            {"code_quality"},
            allow_no_security=False,
            review_type="plan",
        )


def test_enforce_security_rejects_unknown_review_type(cr):
    with pytest.raises(cr.LensArgError, match="unknown review_type"):
        cr.enforce_security_lens(
            set(), allow_no_security=False, review_type="bogus",
        )


# ---------------------------------------------------------------------------
# get_active_members — lens filter
# ---------------------------------------------------------------------------


def test_get_active_members_no_filter(cr, fake_config):
    members = cr.get_active_members(fake_config, "code")
    assert {m["role"] for m in members} == {
        "security", "code_quality", "test_quality", "domain"
    }


def test_get_active_members_with_lens_filter(cr, fake_config):
    members = cr.get_active_members(
        fake_config, "code", lenses={"security", "code_quality"}
    )
    assert {m["role"] for m in members} == {"security", "code_quality"}


# ---------------------------------------------------------------------------
# Tracker v3 migration (R1 #1, #4)
# ---------------------------------------------------------------------------


V2_TRACKER = """# Findings Tracker: Sprint 99 (plan)

Editor: notes

| # | Round | Severity | Lens | Tag | Finding | Status | Resolution |
|---|-------|----------|------|-----|---------|--------|------------|
| 1 | R1 | High | security | injection | SQL injection in login | OPEN |  |
| 2 | R1 | Medium | code_quality | complexity | Long function | ADDRESSED | extracted helpers |
"""


def test_v2_tracker_loads_with_routed_default(cr, tmp_path):
    """R1 #1: a v2 tracker (no Routed column) loads with Routed=[]."""
    path = tmp_path / "FINDINGS_Sprint99.md"
    path.write_text(V2_TRACKER)
    findings = cr._read_tracker(path)
    assert len(findings) == 2
    for f in findings:
        assert f["routed"] == []


def test_tracker_round_trip_preserves_routed(cr, tmp_path):
    """R1 #1: write → read → identical routed list."""
    path = tmp_path / "FINDINGS_Sprint98.md"
    findings = [
        {"id": 1, "round": 1, "severity": "High", "lens": "security",
         "tag": "x", "description": "y", "status": "OPEN", "resolution": "",
         "routed": [1, 3]},
    ]
    cr._write_tracker(path, "98", findings, "plan")
    loaded = cr._read_tracker(path)
    assert loaded[0]["routed"] == [1, 3]


def test_format_routed_column_empty(cr):
    assert cr._format_routed_column([]) == ""


def test_format_routed_column_prefixes_R(cr):
    assert cr._format_routed_column([1, 3, 2]) == "R1,R2,R3"


def test_parse_routed_column_handles_malformed(cr, capsys):
    """A malformed token is skipped with a stderr notice, not fatal."""
    result = cr._parse_routed_column("R1,BADTOKEN,R3")
    assert result == [1, 3]
    err = capsys.readouterr().err
    assert "BADTOKEN" in err


# ---------------------------------------------------------------------------
# Multi-round skipped-lens invariant (R2 recommendation)
# ---------------------------------------------------------------------------


def test_merge_findings_skipped_lens_preserves_state(cr):
    """R1 #4: when a lens is not routed in the current round, its
    findings are left completely untouched."""
    existing = [
        {"id": 1, "round": 1, "severity": "High", "lens": "domain",
         "tag": "x", "description": "domain issue", "status": "OPEN",
         "resolution": "", "routed": [1]},
    ]
    new_findings = [
        {"id": 0, "round": 0, "severity": "High", "lens": "security",
         "tag": "y", "description": "sec issue", "status": "OPEN",
         "resolution": "", "routed": []},
    ]
    merged = cr._merge_findings(
        existing, new_findings, round_num=2,
        routed_lenses={"security"},  # domain skipped
    )
    domain_findings = [f for f in merged if f["lens"] == "domain"]
    assert len(domain_findings) == 1
    assert domain_findings[0]["status"] == "OPEN"  # untouched
    # routed should NOT have been bumped (domain was not routed this round)
    assert 2 not in domain_findings[0]["routed"]
    assert domain_findings[0]["routed"] == [1]


def test_merge_findings_routed_lens_updates_audit(cr):
    """When a lens IS routed, existing findings for that lens get
    their routed list bumped with the current round_num."""
    existing = [
        {"id": 1, "round": 1, "severity": "High", "lens": "security",
         "tag": "x", "description": "sec issue", "status": "OPEN",
         "resolution": "", "routed": [1]},
    ]
    merged = cr._merge_findings(
        existing, [], round_num=3, routed_lenses={"security"},
    )
    assert merged[0]["routed"] == [1, 3]


def test_merge_findings_none_routed_means_all_routed(cr):
    """routed_lenses=None preserves pre-Sprint-6 behaviour: every
    lens is considered routed."""
    existing = [
        {"id": 1, "round": 1, "severity": "High", "lens": "domain",
         "tag": "x", "description": "issue", "status": "ADDRESSED",
         "resolution": "", "routed": [1]},
    ]
    new_findings = [
        {"id": 0, "round": 0, "severity": "High", "lens": "domain",
         "tag": "x", "description": "issue again", "status": "OPEN",
         "resolution": "", "routed": []},
    ]
    # Without routed_lenses, domain's ADDRESSED finding gets reopened.
    merged = cr._merge_findings(existing, new_findings, round_num=2)
    domain_findings = [f for f in merged if f["lens"] == "domain"]
    assert domain_findings[0]["status"] == "REOPENED"


def test_merge_findings_three_round_skipped_lens(cr):
    """Simulate: R1 finds a domain issue; R2 and R3 skip domain.
    After R3, the finding should still be OPEN with routed=[1] —
    unchanged across consecutive skipped rounds."""
    existing = [
        {"id": 1, "round": 1, "severity": "Medium", "lens": "domain",
         "tag": "x", "description": "domain issue", "status": "OPEN",
         "resolution": "", "routed": [1]},
    ]
    r2 = cr._merge_findings(existing, [], round_num=2, routed_lenses={"security"})
    r3 = cr._merge_findings(r2, [], round_num=3, routed_lenses={"security"})
    domain = [f for f in r3 if f["lens"] == "domain"][0]
    assert domain["status"] == "OPEN"
    assert domain["routed"] == [1]


def test_merge_findings_drops_stale_lens_findings(cr):
    """If a consolidator somehow emits findings for a lens that
    wasn't routed this round, the merge drops them rather than
    polluting the tracker."""
    merged = cr._merge_findings(
        [],
        [{"id": 0, "round": 0, "severity": "High", "lens": "domain",
          "tag": "x", "description": "bogus", "status": "OPEN",
          "resolution": "", "routed": []}],
        round_num=2,
        routed_lenses={"security"},
    )
    assert merged == []


# ---------------------------------------------------------------------------
# End-to-end dispatch — R1 #18
# ---------------------------------------------------------------------------


def test_end_to_end_lenses_dispatches_only_selected(cr, fake_config):
    """R1 #18: wire get_active_members together with _resolve_routed_
    lenses and assert the exact member set that would be dispatched
    for an explicit --lenses invocation."""
    import argparse

    ns = argparse.Namespace(
        review_type="code",
        sprint="1",
        title=["t"],
        allow_untracked=False,
        lenses="security,code_quality",
        auto_lenses=False,
        allow_no_security=False,
        verbose=False,
    )
    routed, bypassed = cr._resolve_routed_lenses(ns, fake_config, [])
    assert routed == {"security", "code_quality"}
    assert bypassed is False
    active = cr.get_active_members(fake_config, "code", lenses=routed)
    dispatched = sorted(m["role"] for m in active)
    assert dispatched == ["code_quality", "security"]


def test_end_to_end_auto_lenses_tests_diff(cr, fake_config):
    import argparse

    ns = argparse.Namespace(
        review_type="code",
        sprint="1",
        title=["t"],
        allow_untracked=False,
        lenses=None,
        auto_lenses=True,
        allow_no_security=False,
        verbose=False,
    )
    routed, _ = cr._resolve_routed_lenses(
        ns, fake_config, ["tests/test_foo.py"]
    )
    active = cr.get_active_members(fake_config, "code", lenses=routed)
    dispatched = sorted(m["role"] for m in active)
    assert dispatched == ["code_quality", "security", "test_quality"]


def test_end_to_end_allow_no_security_records_bypass(cr, fake_config):
    import argparse

    ns = argparse.Namespace(
        review_type="code",
        sprint="1",
        title=["t"],
        allow_untracked=False,
        lenses="code_quality",
        auto_lenses=False,
        allow_no_security=True,
        verbose=False,
    )
    routed, bypassed = cr._resolve_routed_lenses(ns, fake_config, [])
    assert bypassed is True
    assert routed == {"code_quality"}


def test_end_to_end_no_lenses_runs_all_members(cr, fake_config):
    """The default path (no routing flags) returns routed_lenses=None,
    and get_active_members returns every member whose phases include
    the review_type."""
    import argparse

    ns = argparse.Namespace(
        review_type="code",
        sprint="1",
        title=["t"],
        allow_untracked=False,
        lenses=None,
        auto_lenses=False,
        allow_no_security=False,
        verbose=False,
    )
    routed, bypassed = cr._resolve_routed_lenses(ns, fake_config, [])
    assert routed is None
    active = cr.get_active_members(fake_config, "code", lenses=routed)
    assert len(active) == 4


# ---------------------------------------------------------------------------
# Sprint argument validation — R1 #15
# ---------------------------------------------------------------------------


def test_sprint_arg_accepts_digits(cr):
    ns = cr._parse_args(["plan", "6", "title"])
    assert ns.sprint == "6"


def test_sprint_arg_rejects_path_traversal(cr):
    import pytest as _pt
    with _pt.raises(SystemExit):
        cr._parse_args(["plan", "../../etc/passwd", "title"])


def test_sprint_arg_rejects_alpha(cr):
    import pytest as _pt
    with _pt.raises(SystemExit):
        cr._parse_args(["plan", "six", "title"])


def test_sprint_arg_rejects_negative(cr):
    import pytest as _pt
    with _pt.raises(SystemExit):
        cr._parse_args(["plan", "-1", "title"])

"""File: tests/test_metrics_digest.py

Purpose: Coverage for scripts/council-metrics-digest.py — deterministic
body, grouping invariants, schema-v2 sentinel discrimination,
members_active=0 contract, v1 fallback, and malformed-row tolerance.

Last updated: Sprint 6 (2026-04-16) -- initial coverage.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGEST_PATH = REPO_ROOT / "scripts" / "council-metrics-digest.py"


def _load_digest_module(repo_root: Path):
    """Import the digest module with REPO_ROOT rebound to a test dir
    so output paths land in tmp_path rather than the real repo."""
    spec = importlib.util.spec_from_file_location(
        f"cmd_{repo_root.name}", DIGEST_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.REPO_ROOT = repo_root
    mod.METRICS_DIR = repo_root / "council"
    mod.ARCHIVE_DIR = repo_root / "Documentation" / "council-metrics-archive"
    mod.OUTPUT = repo_root / "Documentation" / "COUNCIL_METRICS_DIGEST.md"
    return mod


def _stage(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "council").mkdir()
    (proj / "Documentation").mkdir()
    return proj


def _v1_row(**kwargs) -> dict:
    """Row without findings_by_lens / security_bypassed — mimics pre-
    Sprint-6 data."""
    base = {
        "sprint": "3", "review_type": "plan", "round": 1,
        "members_active": 4, "members_succeeded": 4,
        "elapsed_seconds": 120.0,
        "findings_total": 5,
        "findings_high": 1, "findings_medium": 3, "findings_low": 1,
        "findings_addressed": 4, "findings_open": 1,
        "findings_wontfix": 0, "findings_verified": 0,
        "findings_reopened": 0, "findings_recurring": 0,
        "verdict": "APPROVED",
    }
    base.update(kwargs)
    return base


def _v2_row(**kwargs) -> dict:
    base = _v1_row(**kwargs)
    base.setdefault("findings_by_lens", {"security": 2, "code_quality": 3})
    base.setdefault("security_bypassed", False)
    return base


SCHEMA_SENTINEL = {"_schema": "council_metrics", "version": 2}


def _write_metrics(proj: Path, sprint: int, rows: list[dict], *, v2: bool) -> Path:
    path = proj / "council" / f"metrics_Sprint{sprint}.jsonl"
    lines: list[str] = []
    if v2:
        lines.append(json.dumps(SCHEMA_SENTINEL))
    for row in rows:
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n")
    return path


def _strip_timestamp(body: str) -> str:
    return re.sub(r"_Generated [^_]+_", "_TS_", body)


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_input_valid_digest(tmp_path):
    proj = _stage(tmp_path)
    mod = _load_digest_module(proj)
    assert mod.main([]) == 0
    body = mod.OUTPUT.read_text()
    assert "Council Metrics Digest" in body
    assert "_None yet._" in body


# ---------------------------------------------------------------------------
# Deterministic body
# ---------------------------------------------------------------------------


def test_deterministic_body(tmp_path):
    proj = _stage(tmp_path)
    _write_metrics(proj, 4, [_v2_row(sprint="4", review_type="code", round=2)], v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    b1 = mod.OUTPUT.read_text()
    mod.main([])
    b2 = mod.OUTPUT.read_text()
    assert _strip_timestamp(b1) == _strip_timestamp(b2)


# ---------------------------------------------------------------------------
# Mixed v1 + v2 fixture (R2 recommendation)
# ---------------------------------------------------------------------------


def test_mixed_v1_and_v2_discriminate_correctly(tmp_path):
    """A v1 file (no sentinel) loads as v1; a v2 file (with sentinel)
    loads as v2. Both appear in the per-sprint summary."""
    proj = _stage(tmp_path)
    _write_metrics(proj, 3, [_v1_row(sprint="3")], v2=False)
    _write_metrics(proj, 4, [_v2_row(sprint="4")], v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    # Sprint 3 is v1; digest surfaces the schema-v1 notice for lens breakdown.
    assert "| 3 | v1 |" in body
    assert "(schema v1 — no lens breakdown)" in body
    # Sprint 4 is v2; lens counts appear inline.
    assert "| 4 | v2 |" in body
    assert "security:" in body


# ---------------------------------------------------------------------------
# members_active = 0 / missing (R1 #2)
# ---------------------------------------------------------------------------


def test_members_active_zero_renders_na(tmp_path):
    proj = _stage(tmp_path)
    _write_metrics(
        proj, 5,
        [_v2_row(sprint="5", members_active=0, members_succeeded=0)],
        v2=True,
    )
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    assert "| 5 |" in body
    # Success-rate cell must be "n/a", never "0%" (division skipped).
    assert "| n/a |" in body


def test_members_active_missing_renders_na(tmp_path):
    proj = _stage(tmp_path)
    row = _v2_row(sprint="6")
    row.pop("members_active", None)
    _write_metrics(proj, 6, [row], v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    assert "| n/a |" in body


# ---------------------------------------------------------------------------
# Schema sentinel
# ---------------------------------------------------------------------------


def test_sentinel_row_not_counted_as_data(tmp_path):
    """The sentinel row must not contribute findings counts to any
    downstream summary (rounds/elapsed/verdict)."""
    proj = _stage(tmp_path)
    _write_metrics(proj, 7, [_v2_row(sprint="7", round=1)], v2=True)
    mod = _load_digest_module(proj)
    version, rows = mod.load_metrics_file(proj / "council" / "metrics_Sprint7.jsonl")
    assert version == 2
    assert len(rows) == 1
    assert rows[0].get("round") == 1


def test_v1_file_no_sentinel(tmp_path):
    proj = _stage(tmp_path)
    _write_metrics(proj, 8, [_v1_row(sprint="8")], v2=False)
    mod = _load_digest_module(proj)
    version, rows = mod.load_metrics_file(proj / "council" / "metrics_Sprint8.jsonl")
    assert version == 1
    assert len(rows) == 1


def test_malformed_sentinel_skips_file(tmp_path, capsys):
    """A first-line sentinel with an unknown _schema value causes the
    file to be dropped entirely rather than silently reinterpreted."""
    proj = _stage(tmp_path)
    path = proj / "council" / "metrics_Sprint9.jsonl"
    path.write_text(
        json.dumps({"_schema": "something_else", "version": 1}) + "\n"
        + json.dumps(_v1_row(sprint="9")) + "\n"
    )
    mod = _load_digest_module(proj)
    version, rows = mod.load_metrics_file(path)
    assert rows == []
    assert version == 0
    err = capsys.readouterr().err
    assert "unknown _schema value" in err


def test_malformed_row_skipped_not_fatal(tmp_path, capsys):
    proj = _stage(tmp_path)
    path = proj / "council" / "metrics_Sprint10.jsonl"
    path.write_text(
        json.dumps(SCHEMA_SENTINEL) + "\n"
        + "this is not json\n"
        + json.dumps(_v2_row(sprint="10")) + "\n"
    )
    mod = _load_digest_module(proj)
    version, rows = mod.load_metrics_file(path)
    assert version == 2
    assert len(rows) == 1
    err = capsys.readouterr().err
    assert "skipping malformed line" in err


# ---------------------------------------------------------------------------
# Lens activity aggregation (R1 #9: pre-v2 rows without findings_by_lens)
# ---------------------------------------------------------------------------


def test_pre_v2_row_in_v2_file_renders_notice(tmp_path):
    """A v2 file can contain rows missing findings_by_lens (e.g.
    partial migration). The digest falls back to the v1 notice for
    that sprint."""
    proj = _stage(tmp_path)
    row = _v2_row(sprint="11")
    row.pop("findings_by_lens", None)
    _write_metrics(proj, 11, [row], v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    assert "(schema v1 — no lens breakdown)" in body


def test_lens_counts_use_latest_snapshot(tmp_path):
    """R1 #16: findings_by_lens is a cumulative snapshot. The digest
    takes the LAST row's values, not a sum across rounds, so that a
    persistent finding is counted once per sprint, not once per
    round."""
    proj = _stage(tmp_path)
    rows = [
        _v2_row(sprint="12", round=1,
                findings_by_lens={"security": 2, "code_quality": 1}),
        _v2_row(sprint="12", round=2,
                findings_by_lens={"security": 1, "test_quality": 3}),
    ]
    _write_metrics(proj, 12, rows, v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    # Digest reports the R2 (latest) snapshot, not the sum.
    assert "test_quality:3" in body
    assert "security:1" in body
    # code_quality was only in R1's snapshot — absent from latest.
    assert "code_quality" not in body.split("| Lens activity")[1]


# ---------------------------------------------------------------------------
# Security bypass tracking
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Schema-v2 invariants — R1 #19
# ---------------------------------------------------------------------------


def test_lens_counts_do_not_double_count_across_rounds(tmp_path):
    """R1 #16 fix: findings_by_lens is a CUMULATIVE snapshot of the
    tracker. Two rounds emitting the same persistent 1-finding lens
    count must render as 1 in the digest, not 2."""
    proj = _stage(tmp_path)
    rows = [
        _v2_row(sprint="20", round=1, findings_by_lens={"security": 1}),
        _v2_row(sprint="20", round=2, findings_by_lens={"security": 1}),
    ]
    _write_metrics(proj, 20, rows, v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    row_line = next(line for line in body.splitlines() if line.startswith("| 20 "))
    assert "security:1" in row_line
    assert "security:2" not in row_line


def test_success_rate_clamped_above_one(tmp_path):
    """R1 #19: if members_succeeded > members_active (a malformed
    input), the rendered rate must clamp to 100% rather than 125%."""
    proj = _stage(tmp_path)
    _write_metrics(
        proj, 21,
        [_v2_row(sprint="21", members_active=4, members_succeeded=5)],
        v2=True,
    )
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    row_line = next(line for line in body.splitlines() if line.startswith("| 21 "))
    # Should render as 100%, never above.
    assert "| 100% |" in row_line
    assert "125%" not in body


def test_lens_count_total_follows_latest_snapshot(tmp_path):
    """R1 #19 invariant: the final row's findings_by_lens totals
    match the final row's findings_total (both are cumulative
    snapshots of the same tracker)."""
    proj = _stage(tmp_path)
    rows = [
        _v2_row(
            sprint="22", round=2,
            findings_total=4,
            findings_by_lens={"security": 2, "code_quality": 2},
        ),
    ]
    _write_metrics(proj, 22, rows, v2=True)
    mod = _load_digest_module(proj)
    _, loaded_rows = mod.load_metrics_file(
        proj / "council" / "metrics_Sprint22.jsonl"
    )
    final = loaded_rows[-1]
    total_by_lens = sum(final["findings_by_lens"].values())
    assert total_by_lens == final["findings_total"]


def test_security_bypassed_counted(tmp_path):
    proj = _stage(tmp_path)
    rows = [
        _v2_row(sprint="13", round=1, security_bypassed=False),
        _v2_row(sprint="13", round=2, security_bypassed=True),
    ]
    _write_metrics(proj, 13, rows, v2=True)
    mod = _load_digest_module(proj)
    mod.main([])
    body = mod.OUTPUT.read_text()
    row_line = next(line for line in body.splitlines() if line.startswith("| 13 "))
    parts = [p.strip() for p in row_line.split("|")]
    assert parts[-2] == "1", (
        f"security_bypassed count should be 1, row: {row_line!r}"
    )


# ---------------------------------------------------------------------------
# Subprocess smoke (catch import-path regressions)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# JSONL writer + _collect_inputs regressions (R2 #27, #28)
# ---------------------------------------------------------------------------


def test_metrics_sentinel_written_once(tmp_path, council_review_module):
    """R2 #27: _write_metrics_row writes the {"_schema": ...} sentinel
    as the FIRST line of a new file and never emits a second sentinel
    on subsequent appends to the same file."""
    cr = council_review_module
    out = tmp_path / "metrics_Sprint42.jsonl"
    row1 = {"sprint": "42", "round": 1, "findings_total": 0}
    row2 = {"sprint": "42", "round": 2, "findings_total": 1}
    cr._write_metrics_row(out, row1)
    cr._write_metrics_row(out, row2)
    lines = out.read_text().splitlines()
    assert len(lines) == 3  # sentinel + 2 data rows
    first = json.loads(lines[0])
    assert first.get(cr.METRICS_SCHEMA_KEY) == cr.METRICS_SCHEMA_VALUE
    # No other line contains a sentinel.
    for line in lines[1:]:
        obj = json.loads(line)
        assert cr.METRICS_SCHEMA_KEY not in obj


def test_collect_inputs_archive_wins_over_live(tmp_path):
    """R2 #28: when the same sprint has metrics in both the live
    council/ dir AND Documentation/council-metrics-archive/, the
    archive copy wins (it's the durable, post-archival snapshot)."""
    proj = _stage(tmp_path)
    archive = proj / "Documentation" / "council-metrics-archive"
    archive.mkdir()
    # Archive row: 3 findings. Live row: 999 findings. Digest must
    # pick the archive.
    archive_rows = [_v2_row(sprint="30", findings_total=3)]
    live_rows = [_v2_row(sprint="30", findings_total=999)]
    (archive / "metrics_Sprint30.jsonl").write_text(
        json.dumps(SCHEMA_SENTINEL) + "\n"
        + "\n".join(json.dumps(r) for r in archive_rows) + "\n"
    )
    (proj / "council" / "metrics_Sprint30.jsonl").write_text(
        json.dumps(SCHEMA_SENTINEL) + "\n"
        + "\n".join(json.dumps(r) for r in live_rows) + "\n"
    )
    mod = _load_digest_module(proj)
    inputs = mod._collect_inputs()
    # Exactly one file for sprint 30, and it's the archive copy.
    assert len(inputs) == 1
    assert "council-metrics-archive" in str(inputs[0])


def test_subprocess_smoke(tmp_path):
    proj = _stage(tmp_path)
    _write_metrics(proj, 14, [_v2_row(sprint="14")], v2=True)
    shutil.copy2(DIGEST_PATH, proj / "scripts" / "council-metrics-digest.py") \
        if (proj / "scripts").exists() else None
    (proj / "scripts").mkdir(exist_ok=True)
    shutil.copy2(DIGEST_PATH, proj / "scripts" / "council-metrics-digest.py")
    result = subprocess.run(
        [sys.executable, "scripts/council-metrics-digest.py"],
        cwd=proj, capture_output=True, text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (proj / "Documentation" / "COUNCIL_METRICS_DIGEST.md").is_file()

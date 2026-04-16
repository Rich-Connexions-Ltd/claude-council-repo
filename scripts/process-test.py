#!/usr/bin/env python3
"""File: scripts/process-test.py
Purpose: Exercise the full Council review pipeline end-to-end using a throwaway sprint-999 branch and minimal plan so pipeline regressions surface before a real sprint.

Role:
  CI-adjacent smoke test for council-review.py. Creates a disposable git
  branch, writes a trivial plan, runs plan and (optionally) code review
  rounds, records convergence metrics, and cleans up artefacts.
  Supports --plan-only and --cleanup modes.

Last updated: Sprint 123 (2026-04-13) -- initial header block

Process test framework for council review system.

Creates a test branch, writes a minimal plan, runs plan + code review,
measures convergence metrics, and cleans up. Validates the entire review
pipeline end-to-end.

Usage:
    python3 scripts/process-test.py              # Run full process test
    python3 scripts/process-test.py --plan-only  # Run only plan review
    python3 scripts/process-test.py --cleanup    # Clean up any leftover test artifacts
"""

import subprocess
import sys
import time
from pathlib import Path

TEST_SPRINT = "999"
TEST_TITLE = "Process Test: Add Utility Function"


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess command with defaults."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check, **kwargs)


def setup_test_branch() -> str:
    """Create a test branch from current HEAD. Returns branch name."""
    branch = f"process-test-{int(time.time())}"
    run(["git", "checkout", "-b", branch])
    return branch


def write_test_plan(repo_root: Path) -> None:
    """Write a minimal test plan for process validation."""
    plan = f"""# Sprint {TEST_SPRINT}: {TEST_TITLE}

## Problem Statement
Add a simple utility function for formatting durations. This is a process test
to validate the council review pipeline — the utility itself is trivial.

## Spec References
- N/A (process test, no spec requirement)

## Current State
No duration formatting utility exists. This is a self-contained addition.

## Proposed Solution

### Approach
Add a `format_duration()` function to a new module. Chosen for simplicity
and minimal blast radius — no existing code is modified.

### Detailed Design

#### Component 1: format_duration
- **Purpose**: Format seconds into human-readable duration strings
- **Location**: `common/vvp/utils/time_format.py`
- **Interface**: `def format_duration(seconds: float) -> str`
- **Behavior**: Returns strings like "2.5s", "3m 15s", "1h 30m"
- **Edge cases**: Negative values raise ValueError, 0 returns "0.0s"

### Test Strategy
Unit tests covering: zero, fractional seconds, minutes, hours, negative input.

## Files to Create/Modify
| File | Action | Purpose |
|------|--------|---------|
| `common/vvp/utils/time_format.py` | Create | Duration formatting utility |
| `common/tests/test_time_format.py` | Create | Unit tests |

## Open Questions
None.

## Risks and Mitigations
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| None | N/A | N/A | This is a trivial, isolated addition |

## Revision History
| Round | Date | Changes |
|-------|------|---------|
| R1 | {time.strftime("%Y-%m-%d")} | Initial draft (process test) |
"""
    (repo_root / f"PLAN_Sprint{TEST_SPRINT}.md").write_text(plan)


def implement_test_code(repo_root: Path) -> None:
    """Write a simple implementation for code review testing."""
    util_dir = repo_root / "common" / "vvp" / "utils"
    util_dir.mkdir(parents=True, exist_ok=True)

    # Write the utility
    (util_dir / "time_format.py").write_text('''"""Duration formatting utilities (process test artifact)."""


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration string.

    Args:
        seconds: Duration in seconds (must be >= 0).

    Returns:
        Human-readable string like "2.5s", "3m 15s", or "1h 30m".

    Raises:
        ValueError: If seconds is negative.
    """
    if seconds < 0:
        raise ValueError("Duration must be non-negative")
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"
''')

    # Write tests
    test_dir = repo_root / "common" / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_time_format.py").write_text('''"""Tests for format_duration (process test artifact)."""

import pytest
from vvp.utils.time_format import format_duration


def test_zero():
    assert format_duration(0) == "0.0s"


def test_fractional_seconds():
    assert format_duration(2.5) == "2.5s"


def test_seconds():
    assert format_duration(45) == "45.0s"


def test_minutes():
    assert format_duration(150) == "2m 30s"


def test_hours():
    assert format_duration(3661) == "1h 1m"


def test_negative_raises():
    with pytest.raises(ValueError, match="non-negative"):
        format_duration(-1)
''')

    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"Process test: Sprint {TEST_SPRINT} implementation"])


def run_review(review_type: str, repo_root: Path) -> dict:
    """Run council review and measure metrics."""
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "council-review.py"),
         "--allow-external-code-review",
         review_type, TEST_SPRINT, TEST_TITLE],
        capture_output=True, text=True, timeout=300,
    )
    elapsed = time.monotonic() - start

    # Parse verdict and finding count
    review_file = repo_root / f"REVIEW_Sprint{TEST_SPRINT}.md"
    verdict = "UNKNOWN"
    finding_count = 0
    high_count = 0
    if review_file.exists():
        text = review_file.read_text()
        for line in text.splitlines():
            if "**Verdict:**" in line:
                verdict = line.split("**Verdict:**")[1].strip()
            if "**[High]**" in line:
                high_count += 1
            if "**[" in line and "]**" in line:
                finding_count += 1

    # Check findings tracker
    tracker_file = repo_root / f"FINDINGS_Sprint{TEST_SPRINT}.md"
    tracker_exists = tracker_file.exists()

    return {
        "type": review_type,
        "elapsed_seconds": round(elapsed, 1),
        "verdict": verdict,
        "finding_count": finding_count,
        "high_count": high_count,
        "exit_code": result.returncode,
        "tracker_exists": tracker_exists,
        "stderr_snippet": result.stderr[-300:] if result.stderr else "",
        "stdout_snippet": result.stdout[-300:] if result.stdout else "",
    }


def cleanup(repo_root: Path) -> None:
    """Remove test artifacts and return to original branch."""
    # Return to main
    run(["git", "checkout", "main"], check=False)

    # Delete test branches
    result = run(["git", "branch", "--list", "process-test-*"], check=False)
    for branch in result.stdout.strip().split("\n"):
        branch = branch.strip()
        if branch:
            run(["git", "branch", "-D", branch], check=False)

    # Remove test files from working tree
    for pattern in [
        f"PLAN_Sprint{TEST_SPRINT}.md",
        f"REVIEW_Sprint{TEST_SPRINT}.md",
        f"FINDINGS_Sprint{TEST_SPRINT}.md",
        f".sprint-base-commit-{TEST_SPRINT}",
        f".review-round-sprint{TEST_SPRINT}-plan",
        f".review-round-sprint{TEST_SPRINT}-code",
    ]:
        f = repo_root / pattern
        if f.exists():
            f.unlink()

    # Remove test code files
    test_code = repo_root / "common" / "vvp" / "utils" / "time_format.py"
    if test_code.exists():
        test_code.unlink()
    test_file = repo_root / "common" / "tests" / "test_time_format.py"
    if test_file.exists():
        test_file.unlink()

    # Clean council dir
    council_dir = repo_root / "council"
    if council_dir.exists():
        import shutil
        shutil.rmtree(council_dir)


def main():
    repo_root = Path(run(
        ["git", "rev-parse", "--show-toplevel"],
    ).stdout.strip())

    plan_only = "--plan-only" in sys.argv

    if "--cleanup" in sys.argv:
        cleanup(repo_root)
        print("Cleanup complete.")
        return

    print("=" * 60)
    print("Council Review Process Test")
    print("=" * 60)

    results = []
    branch = None

    try:
        # Setup
        print("\n--- Setup ---")
        branch = setup_test_branch()
        print(f"  Test branch: {branch}")

        # Phase 1: Plan review
        print("\n--- Phase 1: Plan Review ---")
        write_test_plan(repo_root)
        print(f"  Plan written: PLAN_Sprint{TEST_SPRINT}.md")

        r = run_review("plan", repo_root)
        results.append(r)
        print(f"  Verdict:  {r['verdict']}")
        print(f"  Findings: {r['finding_count']} ({r['high_count']} High)")
        print(f"  Time:     {r['elapsed_seconds']}s")
        print(f"  Tracker:  {'created' if r['tracker_exists'] else 'not created'}")
        if r['exit_code'] != 0:
            print(f"  ERROR:    Exit code {r['exit_code']}")
            print(f"  stderr:   {r['stderr_snippet']}")

        # Phase 2: Code review (if plan approved and not --plan-only)
        if not plan_only and "APPROVED" in r["verdict"]:
            print("\n--- Phase 2: Code Review ---")
            implement_test_code(repo_root)
            print("  Code committed.")

            r = run_review("code", repo_root)
            results.append(r)
            print(f"  Verdict:  {r['verdict']}")
            print(f"  Findings: {r['finding_count']} ({r['high_count']} High)")
            print(f"  Time:     {r['elapsed_seconds']}s")
            print(f"  Tracker:  {'updated' if r['tracker_exists'] else 'not created'}")
            if r['exit_code'] != 0:
                print(f"  ERROR:    Exit code {r['exit_code']}")
                print(f"  stderr:   {r['stderr_snippet']}")
        elif plan_only:
            print("\n  --plan-only: Skipping code review")
        else:
            print(f"\n  Plan not approved ({r['verdict']}), skipping code review")

        # Report
        print("\n" + "=" * 60)
        print("PROCESS TEST RESULTS")
        print("=" * 60)
        total_time = sum(r["elapsed_seconds"] for r in results)
        total_findings = sum(r["finding_count"] for r in results)
        all_succeeded = all(r["exit_code"] == 0 for r in results)

        print(f"  Status:         {'PASS' if all_succeeded else 'FAIL'}")
        print(f"  Total time:     {total_time:.1f}s")
        print(f"  Total findings: {total_findings}")
        print(f"  Phases run:     {len(results)}")
        print()
        for r in results:
            status = "OK" if r["exit_code"] == 0 else "FAIL"
            print(f"    {r['type']:6s}: [{status}] {r['verdict']:25s} "
                  f"({r['finding_count']} findings, {r['elapsed_seconds']}s)")

        # Feature validation
        print()
        print("  Feature checks:")
        tracker_ok = any(r["tracker_exists"] for r in results)
        print(f"    Findings tracker: {'PASS' if tracker_ok else 'FAIL'}")

        base_commit = (repo_root / f".sprint-base-commit-{TEST_SPRINT}").exists()
        print(f"    Base commit recorded: {'PASS' if base_commit else 'FAIL'}")

    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
    except Exception as e:
        print(f"\n  ERROR: {e}")
    finally:
        print("\n--- Cleanup ---")
        cleanup(repo_root)
        print("  Done.")


if __name__ == "__main__":
    main()

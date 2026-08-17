"""Verdict logic over a promptfoo results JSON — the offline gate's teeth.

  expect-all-pass    : every case must pass every assertion (golden pass set)
  expect-all-fail    : every case must fail >=1 assertion (golden fail set)
  expect-rubric-fail : every case carrying >=1 llm-rubric assert must fail
                       via a rubric component (run with MOCK_JUDGE=fail) —
                       proves each rubric is actually wired to a judge.

The verdict ALWAYS comes from the rows, never promptfoo's exit code (which
is 100 on assert failures — expected on the FAIL set)."""

from __future__ import annotations

import json
from pathlib import Path

MODES = ["expect-all-pass", "expect-all-fail", "expect-rubric-fail"]


def rows_of(results_file: Path) -> list[dict]:
    data = json.loads(Path(results_file).read_text())
    rows = (data.get("results") or {}).get("results") or data.get("results") or []
    return rows if isinstance(rows, list) else []


def _label(row: dict) -> str:
    tc = row.get("testCase") or {}
    return tc.get("description") or row.get("description") or (row.get("vars") or {}).get("golden") or "unnamed case"


def _components(row: dict) -> list[dict]:
    return (row.get("gradingResult") or {}).get("componentResults") or []


def check(results_file: Path, mode: str) -> tuple[int, list[str]]:
    """Returns (cases_checked, problems)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    rows = rows_of(results_file)
    if not rows:
        return 0, [f"no result rows found in {results_file}"]
    problems = []
    checked = 0
    for row in rows:
        success = row.get("success") is True
        if mode == "expect-all-pass":
            checked += 1
            if not success:
                reasons = " | ".join(
                    str(c.get("reason")) for c in _components(row) if not c.get("pass"))
                problems.append(f"SHOULD PASS but failed: {_label(row)}\n    {reasons}")
        elif mode == "expect-all-fail":
            checked += 1
            if success:
                problems.append(
                    f"SHOULD FAIL but passed: {_label(row)} — its asserts caught nothing on the violating bundle")
        else:  # expect-rubric-fail
            rubric = [c for c in _components(row) if (c.get("assertion") or {}).get("type") == "llm-rubric"]
            if not rubric:
                continue
            checked += 1
            if not any(not c.get("pass") for c in rubric):
                problems.append(
                    f"RUBRIC NOT WIRED: {_label(row)} — MOCK_JUDGE=fail did not fail any of its "
                    f"{len(rubric)} rubric assert(s)")
    return checked, problems

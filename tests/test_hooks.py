"""case_end classification: the breaker must only count infra errors.

Regression for run graded-20260815-133553 — suite 03's graded baseline was
killed at 60% "error rate" when every "error" was a judge-scored
below-threshold case (promptfoo copies assert-failure reasons into
result.error, failureReason=ASSERT)."""

from __future__ import annotations

import json
from pathlib import Path

from src import hooks
from src.utils import status
from src.utils.proc import _breaker_tripped

FR_NONE, FR_ASSERT, FR_ERROR = 0, 1, 2


def _run_case(tmp_path: Path, monkeypatch, result: dict) -> dict:
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    hooks._after_each({
        "test": {"vars": {"cell": "03-diagram·clean-python-rushed"}},
        "result": result,
    })
    events = [r for r in status.read(tmp_path) if r["ev"] == "case_end"]
    assert len(events) == 1
    return events[0]


def test_assert_failure_is_not_an_infra_error(tmp_path, monkeypatch):
    ev = _run_case(tmp_path, monkeypatch, {
        "success": False,
        "failureReason": FR_ASSERT,
        "error": "fidelity 2 · readability 5 | invents a Depot Manifest Service",
        "gradingResult": {"pass": False},
        "response": {"cached": False},
    })
    assert ev["ok"] is True          # breaker must not count it
    assert ev["passed"] is False     # but the graded fail stays visible
    assert "fidelity 2" in ev["note"]


def test_provider_error_is_an_infra_error(tmp_path, monkeypatch):
    ev = _run_case(tmp_path, monkeypatch, {
        "success": False,
        "failureReason": FR_ERROR,
        "error": "API error: overloaded_error",
        "response": {},
    })
    assert ev["ok"] is False
    assert ev["passed"] is False


def test_clean_pass(tmp_path, monkeypatch):
    ev = _run_case(tmp_path, monkeypatch, {
        "success": True,
        "failureReason": FR_NONE,
        "error": None,
        "gradingResult": {"pass": True},
        "response": {"cached": True},
    })
    assert ev["ok"] is True
    assert ev["passed"] is True


def test_legacy_fallback_without_failure_reason(tmp_path, monkeypatch):
    # No failureReason field: an error WITH a gradingResult is a graded fail,
    # an error WITHOUT one is infra.
    ev = _run_case(tmp_path, monkeypatch, {
        "success": False,
        "error": "scored below threshold",
        "gradingResult": {"pass": False},
        "response": {},
    })
    assert ev["ok"] is True

    other = tmp_path / "other"
    ev2 = _run_case(other, monkeypatch, {
        "success": False,
        "error": "ECONNRESET",
        "response": {},
    })
    assert ev2["ok"] is False


def test_breaker_ignores_graded_fails(tmp_path):
    graded_fails = [{"ev": "case_end", "ok": True, "passed": False}] * 10
    assert _breaker_tripped(graded_fails, {}) is None

    infra = [{"ev": "case_end", "ok": False}] * 4
    assert _breaker_tripped(graded_fails + infra, {}) == "4 consecutive case errors/truncations"


def test_truncated_terminal_reason_marks_the_case(tmp_path, monkeypatch):
    ev = _run_case(tmp_path, monkeypatch, {
        "success": True,
        "failureReason": FR_NONE,
        "gradingResult": {"pass": True},
        "response": {"cached": False, "metadata": {"terminalReason": "max_tokens"}},
    })
    assert ev["truncated"] is True
    assert ev["ok"] is True  # not an infra error — the breaker counts it separately


def test_cached_replay_is_never_marked_truncated(tmp_path, monkeypatch):
    ev = _run_case(tmp_path, monkeypatch, {
        "success": True,
        "failureReason": FR_NONE,
        "gradingResult": {"pass": True},
        "response": {"cached": True, "metadata": {"terminalReason": "max_tokens"}},
    })
    assert ev["truncated"] is False


def test_token_cap_hit_marks_truncated(tmp_path, monkeypatch):
    from src import config
    cap = int(config.agents()["generation"].get("max_tokens_per_turn", 64000))
    ev = _run_case(tmp_path, monkeypatch, {
        "success": True,
        "failureReason": FR_NONE,
        "gradingResult": {"pass": True},
        "response": {"cached": False, "cost": 0.01,
                     "tokenUsage": {"prompt": 100, "completion": cap}},
    })
    assert ev["truncated"] is True


def test_breaker_counts_truncations_by_default():
    truncated = [{"ev": "case_end", "ok": True, "truncated": True}] * 4
    assert _breaker_tripped(truncated, {}) == "4 consecutive case errors/truncations"
    assert _breaker_tripped(truncated, {"count_truncations": False}) is None


def test_breaker_truncation_rate():
    events = ([{"ev": "case_end", "ok": True, "truncated": True}] * 6
              + [{"ev": "case_end", "ok": True}] * 4)
    # 60% truncated over 10 cases > default 50% error rate
    assert "rate" in (_breaker_tripped(events, {}) or "")
    assert _breaker_tripped(events, {"count_truncations": False}) is None

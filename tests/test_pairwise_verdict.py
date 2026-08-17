"""compare_verdict statistics + promotion bar — all offline.

The sign test / CI are REPORTED, never gated on (small-n honesty, audit
finding 2); the pairwise bar (min_pairwise_wins, default 2) IS the gate.
"""

import json

import pytest

from src import config
from src.science import pairwise

DIMS = ("warmth", "clarity", "usefulness")


def _row(suite, cell, variant, output, scores, trial=1):
    return {
        "testCase": {"vars": {"suite": suite, "cell": cell, "promptVariant": variant,
                              "promptFile": "demo.md", "trial": trial}},
        "response": {"output": output},
        "gradingResult": {"componentResults": [{"namedScores": dict(scores), "pass": True}]},
        "success": True,
    }


def _write_results(tmp_path, suite, cells):
    """cells: list of (cell_id, current_scores, candidate_scores,
    candidate_marker) — marker text lets a fake pairwise judge recognize the
    candidate bundle blind."""
    rows = []
    for cell_id, cur, cand, marker in cells:
        rows.append(_row(suite, cell_id, "current", f"current output {cell_id}", cur))
        rows.append(_row(suite, cell_id, "candidate", f"candidate {marker} {cell_id}", cand))
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"results": {"results": rows}}))
    return path


# ------------------------------------------------------------ pure helpers —
def test_sign_test_two_sided_exact():
    assert pairwise._sign_test_p(0, 0) is None
    assert pairwise._sign_test_p(1, 0) == 1.0
    assert abs(pairwise._sign_test_p(6, 0) - 0.03125) < 1e-9
    assert pairwise._sign_test_p(3, 3) == 1.0
    assert pairwise._sign_test_p(2, 5) == pairwise._sign_test_p(5, 2)


def test_paired_ci_t_interval():
    assert pairwise._paired_delta_ci95([1.0]) is None
    lo, hi = pairwise._paired_delta_ci95([1.0, 2.0, 3.0])
    # mean 2, se 1/sqrt(3), t(df=2) = 4.303
    assert abs(lo - (2 - 4.303 / 3 ** 0.5)) < 1e-2
    assert abs(hi - (2 + 4.303 / 3 ** 0.5)) < 1e-2


# --------------------------------------------------------- promotion gates —
def _fake_pairwise(suite_id, rubric, a, b):
    if "WINNER" in a:
        return {"winner": "A", "reason": "mock"}
    if "WINNER" in b:
        return {"winner": "B", "reason": "mock"}
    return {"winner": "tie", "reason": "mock"}


def test_default_bar_rejects_a_single_pairwise_win(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "WINNER"),   # candidate wins both orders
        ("c2", low, high, "plain"),    # tie
        ("c3", low, high, "plain"),    # tie
    ])
    record = pairwise.compare_verdict(results, "warmth", log=lambda *a: None)
    assert record["pairwise"]["winsCandidate"] == 1
    assert record["pairwise"]["minWins"] == 2
    assert record["promote"] is False  # target gain +1.0 is fine; the bar is not


def test_min_pairwise_wins_configurable(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    monkeypatch.setitem(config.get()["graded"], "promotion",
                        {"min_pairwise_wins": 1, "min_cells": 2})
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "WINNER"),
        ("c2", low, high, "plain"),
    ])
    record = pairwise.compare_verdict(results, "warmth", log=lambda *a: None)
    assert record["promote"] is True


# ------------------------------------------------------- reported, not gated —
def test_verdict_reports_sign_test_and_ci(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_PAIRWISE", "tie")
    low = {d: 3 for d in DIMS}
    high = {"warmth": 5, "clarity": 4, "usefulness": 4}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "plain"),
        ("c2", low, low, "plain"),
    ])
    lines = []
    record = pairwise.compare_verdict(results, "warmth", log=lines.append)
    stats = record["stats"]
    assert stats["signTestP"] is None  # all cells tied — no signal
    assert stats["nonTieCells"] == 0
    assert stats["ciCells"] == 2
    lo, hi = stats["targetDeltaCI95"]  # deltas [2, 0] → mean 1, wide interval
    assert lo < 1 < hi
    text = "\n".join(lines)
    assert "sign test on cell wins: all cells tied" in text
    assert "target Δ 95% CI" in text
    assert "reported, not gated" in text


def test_sign_test_p_printed_and_stored_with_wins(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    monkeypatch.setitem(config.get()["graded"], "promotion", {"min_cells": 2})
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "WINNER"),
        ("c2", low, high, "WINNER"),
    ])
    lines = []
    record = pairwise.compare_verdict(results, "warmth", log=lines.append)
    assert record["stats"]["signTestP"] == pytest.approx(0.5)  # 2-0, two-sided
    assert record["stats"]["nonTieCells"] == 2
    assert record["promote"] is True  # meets the default bar of 2
    assert any("p = 0.500 (2 non-tie cells)" in ln for ln in lines)


# ---------------------------------------------------- verdict budget gate —
def test_verdict_budget_gate_aborts_before_any_judge_call(synthetic_project, tmp_path,
                                                          monkeypatch, capsys):
    import argparse

    from src import runner
    from src.evaluators import llm_judge
    monkeypatch.setattr(llm_judge, "judge_call",
                        lambda *a, **kw: pytest.fail("judge must not be called past the gate"))
    low = {d: 3 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [("c1", low, low, "plain"),
                                                      ("c2", low, low, "plain")])
    rc = runner.cmd_verdict(argparse.Namespace(
        results=str(results), target="warmth", reason=None, new_evidence=None,
        max_cost=0.001, force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ABORTED BEFORE ANY CALL" in err
    assert "4 judge calls" in err  # 2 cells x both orders


def test_verdict_gate_skipped_under_mock_pairwise(synthetic_project, tmp_path, monkeypatch):
    import argparse

    from src import runner
    monkeypatch.setenv("MOCK_PAIRWISE", "tie")
    low = {d: 3 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [("c1", low, low, "plain")])
    rc = runner.cmd_verdict(argparse.Namespace(
        results=str(results), target="warmth", reason=None, new_evidence=None,
        max_cost=0.0001, force=False))
    # 1 cell < min_cells 3 → INSUFFICIENT_EVIDENCE (exit 2), still recorded
    assert rc == 2
    assert list(config.history_dir().glob("compare-demo-suite-*.json"))


def _mkdir(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------- E10: min-cells gate + INSUFFICIENT_EVIDENCE —
def test_undersized_compare_is_insufficient_and_free(synthetic_project, tmp_path, monkeypatch):
    calls = []

    def counting_pairwise(suite_id, rubric, a, b):
        calls.append(1)
        return {"winner": "B", "reason": "mock"}

    monkeypatch.setattr(pairwise, "_pairwise_call", counting_pairwise)
    low = {d: 3 for d in DIMS}
    high = {d: 5 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "plain"), ("c2", low, high, "plain")])
    record = pairwise.compare_verdict(results, "warmth", log=lambda *a: None)
    assert record["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert record["promote"] is False
    assert calls == []  # zero paid pairwise calls below min_cells


def test_insufficient_evidence_does_not_block_a_later_promote(synthetic_project, tmp_path, monkeypatch):
    """IE is absence of evidence, not evidence against: repeating on the same
    key requires --reason but NOT --new-evidence (unlike after a REJECT)."""
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    small = _write_results(_mkdir(tmp_path / "small"), "demo-suite", [
        ("c1", low, high, "WINNER"), ("c2", low, high, "WINNER")])
    record = pairwise.compare_verdict(small, "warmth", log=lambda *a: None)
    assert record["verdict"] == "INSUFFICIENT_EVIDENCE"
    big = _write_results(_mkdir(tmp_path / "big"), "demo-suite", [
        ("c1", low, high, "WINNER"), ("c2", low, high, "WINNER"),
        ("c3", low, high, "WINNER")])
    record2 = pairwise.compare_verdict(big, "warmth", reason="larger matrix",
                                       log=lambda *a: None)
    assert record2["verdict"] == "PROMOTE"  # no --new-evidence needed


def test_reject_still_blocks_a_rerolled_promote(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    losing = _write_results(_mkdir(tmp_path / "a"), "demo-suite", [
        ("c1", high, low, "plain"), ("c2", low, low, "plain"),
        ("c3", low, low, "plain")])
    r1 = pairwise.compare_verdict(losing, "warmth", log=lambda *a: None)
    assert r1["verdict"] == "REJECT"
    winning = _write_results(_mkdir(tmp_path / "b"), "demo-suite", [
        ("c1", low, high, "WINNER"), ("c2", low, high, "WINNER"),
        ("c3", low, high, "WINNER")])
    r2 = pairwise.compare_verdict(winning, "warmth", reason="retry",
                                  log=lambda *a: None)
    assert r2["verdict"] == "REJECT" and r2["blocked"]


def test_max_sign_p_turns_thin_promote_into_insufficient(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(pairwise, "_pairwise_call", _fake_pairwise)
    monkeypatch.setitem(config.get()["graded"], "promotion",
                        {"min_cells": 3, "max_sign_p": 0.05})
    low = {d: 3 for d in DIMS}
    high = {d: 4 for d in DIMS}
    results = _write_results(tmp_path, "demo-suite", [
        ("c1", low, high, "WINNER"), ("c2", low, high, "WINNER"),
        ("c3", low, high, "WINNER")])
    record = pairwise.compare_verdict(results, "warmth", log=lambda *a: None)
    # 3-0 → p = 0.25 > 0.05: candidate leads but the evidence is too thin
    assert record["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert record["promote"] is False

"""E12: human spot-check of judge verdicts — pure-artifact, zero API calls."""

import json

import pytest

from src import config, runner
from src.science import spot_check as sc

SUITE = "demo-suite"
DIMS = ("warmth", "clarity", "usefulness")


def _fabricate_run(tmp_path, cells=("c1", "c2", "c3"), score=4):
    run_dir = tmp_path / "graded-20260815-101010"
    run_dir.mkdir()
    evidence, rows = [], []
    for cell in cells:
        scores = {d: score for d in DIMS}
        evidence.append({"suite": SUITE, "cell": cell, "judgeModel": "claude-sonnet-4-6",
                         "scores": scores,
                         "judge": {"top_issue": "none", "top_issue_tag": "other"},
                         "vars": {"trial": 1}})
        rows.append({"testCase": {"vars": {"suite": SUITE, "cell": cell, "trial": 1}},
                     "response": {"output": f"output text for {cell}"},
                     "success": True})
    (run_dir / "judge-evidence.jsonl").write_text(
        "\n".join(json.dumps(e) for e in evidence) + "\n")
    (run_dir / "results.json").write_text(json.dumps({"results": {"results": rows}}))
    return run_dir


def test_sampling_is_deterministic_and_spread(synthetic_project, tmp_path):
    run_dir = _fabricate_run(tmp_path)
    s1 = sc.sample_cells(run_dir, 2)
    s2 = sc.sample_cells(run_dir, 2)
    assert [x["cell"] for x in s1] == [x["cell"] for x in s2]
    assert len({x["cell"] for x in s1}) == 2  # spread across cells


def test_spot_check_records_chained_labels_and_agreement(synthetic_project, tmp_path):
    run_dir = _fabricate_run(tmp_path, score=4)  # judge pass on all (threshold 3)
    answers = iter([True] * 6 + [False] * 3)  # 3 cells x 3 dims
    result = sc.spot_check(run_dir, 3, lambda dim, s: next(answers),
                           log=lambda *a: None)
    assert result["labels"] == 9
    assert result["agreement"] == pytest.approx(6 / 9)
    from src.utils import chain
    assert chain.verify_chain(sc.checks_path()) == []
    entries = [json.loads(l) for l in sc.checks_path().read_text().splitlines()]
    assert len(entries) == 9
    assert all(e["judgeModel"] == "claude-sonnet-4-6" for e in entries)


def test_rolling_agreement_filters_on_current_judge(synthetic_project, tmp_path):
    run_dir = _fabricate_run(tmp_path)
    sc.spot_check(run_dir, 3, lambda dim, s: True, log=lambda *a: None)
    rolling = sc.rolling_agreement()
    assert rolling["labels"] == 9 and rolling["agreement"] == 1.0
    # labels from a different judge model don't count
    from src.utils import chain
    chain.append_chained(sc.checks_path(), {"judgeModel": "openai:gpt-x",
                                            "agree": False, "dim": "warmth"})
    assert sc.rolling_agreement()["labels"] == 9


def test_agreement_warning_floor(synthetic_project, tmp_path, monkeypatch):
    run_dir = _fabricate_run(tmp_path, cells=tuple(f"c{i}" for i in range(12)))
    # 12 cells x 3 dims = 36 labels, human disagrees with everything
    sc.spot_check(run_dir, 12, lambda dim, s: False, log=lambda *a: None)
    warning = sc.agreement_warning()
    assert warning is not None and "below" in warning
    # under 30 labels: no warning even at 0%
    sc.checks_path().unlink()
    (tmp_path / "second").mkdir()
    run2 = _fabricate_run(tmp_path / "second")
    sc.spot_check(run2, 3, lambda dim, s: False, log=lambda *a: None)
    assert sc.agreement_warning() is None


def test_cmd_spot_check_with_canned_answers(synthetic_project, tmp_path, capsys, monkeypatch):
    run_dir = _fabricate_run(tmp_path)
    monkeypatch.setenv("EVAL_SPOT_ANSWERS", "y" * 9)
    rc = runner.main(["spot-check", "--run", str(run_dir), "--n", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "agreement 100%" in out
    assert "rolling" in out
    # Per-dimension agreement must be surfaced, not just computed — a judge can
    # drift badly on one dimension while the overall rate stays green.
    assert ": 100%" in out


def test_cmd_spot_check_refuses_empty_run(synthetic_project, tmp_path, capsys, monkeypatch):
    empty = tmp_path / "graded-empty"
    empty.mkdir()
    monkeypatch.setenv("EVAL_SPOT_ANSWERS", "y")
    rc = runner.main(["spot-check", "--run", str(empty)])
    assert rc == 1
    assert "SPOT-CHECK REFUSED" in capsys.readouterr().err

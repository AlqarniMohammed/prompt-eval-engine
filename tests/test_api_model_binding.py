"""E8: calibration bound to the provider-reported model id + self-consistency."""

import json

import pytest

from src import config, runner, state
from src.science.calibrate import calibrate_suite, consistency_of

SUITE = "demo-suite"


def test_consistency_of_math():
    goldens = {
        "demo/pass.txt": {"set": "pass", "byDim": {"warmth": [5, 5, 5], "clarity": [4, 5, 4]}},
        "demo/fail.txt": {"set": "fail", "byDim": {"warmth": [1, 1, 1]}},
    }
    c = consistency_of(goldens)
    assert c["pairs"] == 3
    assert c["exactAgreement"] == pytest.approx(2 / 3)
    assert c["meanSpread"] == pytest.approx(1 / 3)
    assert consistency_of({}) == {"pairs": 0, "exactAgreement": None, "meanSpread": None}


def test_calibration_record_carries_api_model_and_consistency(synthetic_project, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "good")
    run_dir = tmp_path / "calibrate-x"
    run_dir.mkdir()
    (run_dir / "spend.jsonl").write_text(json.dumps(
        {"kind": "judge", "usd": 0.01, "apiModel": "claude-sonnet-4-6-20250929",
         "at": "2026-08-15T00:00:00+00:00"}) + "\n")
    monkeypatch.setenv("EVAL_RUN_DIR", str(run_dir))
    record = calibrate_suite(config.suite_by_id(SUITE), log=lambda *a: None)
    assert record["judgeApiModel"] == "claude-sonnet-4-6-20250929"
    assert record["consistency"]["pairs"] > 0
    assert record["consistency"]["exactAgreement"] == 1.0  # mock scores are constant
    assert state.suite_state(SUITE)["calibrated"]["api_model"] == "claude-sonnet-4-6-20250929"


def test_drift_flag_gates_the_next_run(synthetic_project, tmp_path, capsys):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE),
                                       "green": True, "api_model": "snap-june"})
    run_dir = tmp_path / "graded-x"
    run_dir.mkdir()
    (run_dir / "spend.jsonl").write_text(json.dumps(
        {"kind": "judge", "usd": 0.01, "apiModel": "snap-august",
         "at": "2026-08-15T00:00:00+00:00"}) + "\n")
    runner._check_judge_api_drift(run_dir, [SUITE])
    err = capsys.readouterr().err
    assert "drift" in err.lower() or "moved the model" in err
    entry = state.suite_state(SUITE)["calibrated"]
    assert entry["apiModelDrift"] == "snap-august"
    with pytest.raises(state.StateError, match="recalibrate"):
        state.require(SUITE, "graded")
    # a fresh calibration record clears the flag
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE),
                                       "green": True, "api_model": "snap-august"})
    assert "apiModelDrift" not in state.suite_state(SUITE)["calibrated"]


def test_drift_check_is_silent_when_ids_match(synthetic_project, tmp_path, capsys):
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE),
                                       "green": True, "api_model": "snap-august"})
    run_dir = tmp_path / "graded-x"
    run_dir.mkdir()
    (run_dir / "spend.jsonl").write_text(json.dumps(
        {"kind": "judge", "usd": 0.01, "apiModel": "snap-august",
         "at": "2026-08-15T00:00:00+00:00"}) + "\n")
    runner._check_judge_api_drift(run_dir, [SUITE])
    assert capsys.readouterr().err == ""
    assert "apiModelDrift" not in state.suite_state(SUITE)["calibrated"]

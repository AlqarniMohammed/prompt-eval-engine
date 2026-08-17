import pytest

from src import state


@pytest.fixture(autouse=True)
def isolated_state(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_path", lambda: tmp_path / ".state.json")
    state.record_preflight(state.preflight_fingerprint())


SUITE = "demo-suite"


def test_gate_refuses_without_predecessors():
    with pytest.raises(state.StateError, match="never been recorded"):
        state.require(SUITE, "graded")


def test_missing_preflight_blocks_campaigns(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_path", lambda: tmp_path / "other" / ".state.json")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    with pytest.raises(state.StateError, match="preflight has never been run"):
        state.require(SUITE, "graded")


def test_stale_preflight_blocks_after_model_change(monkeypatch):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    monkeypatch.setenv("EVAL_MODEL", "claude-opus-5")
    # calibration fingerprint is judge-side, so only preflight goes stale
    with pytest.raises(state.StateError, match="preflight is STALE"):
        state.require(SUITE, "graded")


def test_full_green_path():
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    assert state.require(SUITE, "graded") == []
    state.record(SUITE, "baselined", {"runId": "graded-x"})
    assert state.require(SUITE, "compare") == []
    state.record(SUITE, "compared", {"verdict": "PROMOTE"})
    assert state.require(SUITE, "promote") == []


def test_non_green_calibration_blocks():
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": False})
    with pytest.raises(state.StateError, match="NOT green"):
        state.require(SUITE, "graded")


def test_stale_fingerprint_blocks():
    fp = state.validate_fingerprint(SUITE)
    state.record(SUITE, "validated", {**fp, "dataset_sha": "deadbeef"})
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    with pytest.raises(state.StateError, match="STALE"):
        state.require(SUITE, "graded")


def test_judge_model_change_invalidates_calibration(monkeypatch):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "claude-opus-5")
    with pytest.raises(state.StateError, match="STALE"):
        state.require(SUITE, "graded")


def test_judge_temperature_change_invalidates_calibration(monkeypatch):
    from src import config
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    monkeypatch.setitem(config.get()["agents"]["judge"], "temperature", 1)
    with pytest.raises(state.StateError, match="STALE"):
        state.require(SUITE, "graded")


def test_force_returns_problems_instead_of_raising():
    problems = state.require(SUITE, "graded", force=True)
    assert problems and any("never been recorded" in p for p in problems)


def test_earlier_stage_invalidates_later_ones():
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    state.record(SUITE, "baselined", {"runId": "r1"})
    # re-validate after content moved (prompt surgery) — successors must fall
    moved = {**state.validate_fingerprint(SUITE), "dataset_sha": "moved"}
    state.record(SUITE, "validated", moved)
    assert "baselined" not in state.suite_state(SUITE)


def test_rerecording_identical_data_preserves_successors(tmp_path, monkeypatch):
    # Regression: `validate` (free, run-at-every-step) re-recorded `validated`
    # and wiped live calibrations even when nothing had changed.
    from src import state

    monkeypatch.setattr(state, "_state_path", lambda: tmp_path / "state.json")
    fp = {"dataset_sha": "abc", "asserts_sha": "def", "config_sha": "ghi"}
    state.record("s", "validated", fp)
    state.record("s", "calibrated", {"rubric_sha": "r1", "judge": "j1"})
    state.record("s", "validated", fp)  # identical re-validate
    assert "calibrated" in state.suite_state("s"), "identical re-validate must keep calibration"
    state.record("s", "validated", {**fp, "dataset_sha": "CHANGED"})
    assert "calibrated" not in state.suite_state("s"), "changed content must still invalidate"


def test_unknown_suite_raises_named_state_error():
    with pytest.raises(state.StateError, match='unknown suite "nope"'):
        state.validate_fingerprint("nope")
    with pytest.raises(state.StateError, match="configured suites: demo-suite"):
        state.calibration_fingerprint("nope")

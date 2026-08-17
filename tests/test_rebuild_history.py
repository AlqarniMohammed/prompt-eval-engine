"""E2: atomic state writes, snapshot rotation, rebuild-from-history, and the
calibration temperature-fingerprint fix."""

import json

from src import config, state
from src.science import calibrate

SUITE = "demo-suite"


def _hist() -> "config.Path":
    d = config.history_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_validated(sid=SUITE):
    state.record(sid, "validated", state.validate_fingerprint(sid))


def _write_calibration(judge_temperature=0, green=True, rubric_sha=None, name=None):
    rec = {"suite": SUITE, "green": green,
           "judgeModel": config.agents()["judge"]["model"],
           "rubricSha256": rubric_sha or state.calibration_fingerprint(SUITE)["rubric_sha"],
           "judgeTemperature": judge_temperature}
    if judge_temperature == "omit":
        rec.pop("judgeTemperature")
    (_hist() / (name or f"calibration-{SUITE}-20260101-000000.json")).write_text(json.dumps(rec))


def test_save_is_atomic_and_leaves_no_tmp(synthetic_project):
    _record_validated()
    state_path = config.outputs_root() / ".state.json"
    assert state_path.exists()
    assert not state_path.with_name(".state.json.tmp").exists()
    json.loads(state_path.read_text())  # valid, complete JSON


def test_snapshot_rotates_dated_copies(synthetic_project):
    _record_validated()
    snap_dir = config.outputs_root() / ".state-snapshots"
    for _ in range(state.SNAPSHOT_KEEP + 3):
        state.snapshot()
    snaps = list(snap_dir.glob("state-*.json"))
    assert len(snaps) == state.SNAPSHOT_KEEP


def test_calibrate_records_the_temperature_fingerprint(synthetic_project, monkeypatch):
    # The regression: calibrate_suite recorded no judge_temperature, so any
    # suite with a configured judge temperature was instantly STALE at the
    # next gate.
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "good")
    calibrate.calibrate_suite(config.suite_by_id(SUITE), log=lambda *a: None)
    entry = state.suite_state(SUITE)["calibrated"]
    want = state.calibration_fingerprint(SUITE)
    assert entry["judge_temperature"] == want["judge_temperature"] == 0
    assert entry["rubric_sha"] == want["rubric_sha"]


def test_rebuild_requires_validated_first(synthetic_project):
    _write_calibration()
    lines = state.rebuild_from_history()
    assert any("SKIPPED" in ln and "validate" in ln for ln in lines)
    assert "calibrated" not in state.suite_state(SUITE)


def test_rebuild_restores_matching_calibration(synthetic_project):
    _record_validated()
    _write_calibration()
    lines = state.rebuild_from_history()
    entry = state.suite_state(SUITE)["calibrated"]
    assert entry["restored_from"] == "history"
    assert entry["green"] is True
    assert any(f"{SUITE}: calibrated restored" in ln for ln in lines)


def test_rebuild_refuses_moved_rubric_and_legacy_temperature(synthetic_project):
    _record_validated()
    _write_calibration(rubric_sha="not-the-current-sha", name=f"calibration-{SUITE}-20260101-000001.json")
    _write_calibration(judge_temperature="omit", name=f"calibration-{SUITE}-20260101-000002.json")
    lines = state.rebuild_from_history()
    assert "calibrated" not in state.suite_state(SUITE)
    assert any("calibrated NOT restored" in ln for ln in lines)


def test_rebuild_full_chain(synthetic_project):
    _record_validated()
    _write_calibration()
    agents = config.agents()
    prod = config.resolve(config.get()["prompts"]["production_dir"]) / "demo.md"
    prod_sha = state.sha256_file(prod)
    (_hist() / "graded-20260101-000000.json").write_text(json.dumps({
        "manifest": {"runId": "graded-20260101-000000", "suite": SUITE,
                     "models": {"generation": agents["generation"]["model"],
                                "judge": agents["judge"]["model"]},
                     "promptSha256": {"demo.md": prod_sha}},
        "cases": [{"suite": SUITE, "success": True}],
    }))
    (_hist() / f"compare-{SUITE}-20260102-000000.json").write_text(json.dumps({
        "suite": SUITE, "promote": True, "target": "warmth",
        "rubricSha256": state.calibration_fingerprint(SUITE)["rubric_sha"],
        "judgeModel": agents["judge"]["model"],
        "currentPromptSha256": prod_sha,
        # candidate was promoted, so its sha IS the current production sha
        "candidatePromptSha256": prod_sha,
    }))
    (_hist() / "confirm-20260103-000000.json").write_text(json.dumps({
        "manifest": {"runId": "confirm-20260103-000000"},
        "cases": [{"suite": SUITE, "success": True}, {"suite": SUITE, "success": True}],
    }))
    lines = state.rebuild_from_history()
    st = state.suite_state(SUITE)
    assert all(stage in st for stage in
               ("validated", "calibrated", "baselined", "compared", "promoted", "confirmed"))
    assert st["compared"]["verdict"] == "PROMOTE"
    assert st["promoted"]["promptFile"] == "demo.md"
    assert st["confirmed"]["rows"] == 2
    assert sum("restored" in ln for ln in lines) >= 5


def test_rebuild_refuses_stale_baseline_prompts(synthetic_project):
    _record_validated()
    _write_calibration()
    agents = config.agents()
    (_hist() / "graded-20260101-000000.json").write_text(json.dumps({
        "manifest": {"runId": "graded-x", "suite": SUITE,
                     "models": {"generation": agents["generation"]["model"],
                                "judge": agents["judge"]["model"]},
                     "promptSha256": {"demo.md": "sha-of-an-older-prompt"}},
        "cases": [{"suite": SUITE, "success": True}],
    }))
    lines = state.rebuild_from_history()
    assert "baselined" not in state.suite_state(SUITE)
    assert any("baselined NOT restored" in ln for ln in lines)

"""The confirm stage must actually be recordable — and a verdict computed over
a confirm run's results must not wipe the earned `promoted` stage."""

import json

from src import runner, state
from src.science import pairwise


def _row(suite: str, ok: bool) -> dict:
    return {"testCase": {"vars": {"suite": suite}}, "success": ok}


def test_clean_confirm_records_confirmed(synthetic_project):
    sid = synthetic_project.suite_id
    failed = runner._record_run_stages(
        "confirm", sid, {"runId": "confirm-x"}, [_row(sid, True), _row(sid, True)], None)
    assert failed == []
    assert state.suite_state(sid)["confirmed"]["runId"] == "confirm-x"
    assert state.suite_state(sid)["confirmed"]["rows"] == 2


def test_confirm_with_findings_records_nothing(synthetic_project):
    sid = synthetic_project.suite_id
    failed = runner._record_run_stages(
        "confirm", sid, {"runId": "confirm-x"}, [_row(sid, True), _row(sid, False)], None)
    assert failed == [sid]
    assert "confirmed" not in state.suite_state(sid)


def test_confirm_subset_records_nothing(synthetic_project):
    sid = synthetic_project.suite_id
    failed = runner._record_run_stages(
        "confirm", sid, {"runId": "confirm-x"}, [_row(sid, True)], 2)
    assert failed == []
    assert "confirmed" not in state.suite_state(sid)


def test_multi_suite_confirm_records_per_suite(synthetic_project):
    rows = [_row("demo-suite", True), _row("demo-suite-2", False)]
    failed = runner._record_run_stages("confirm", None, {"runId": "confirm-x"}, rows, None)
    assert failed == ["demo-suite-2"]
    assert "confirmed" in state.suite_state("demo-suite")
    assert "confirmed" not in state.suite_state("demo-suite-2")


def test_graded_stage_recording_unchanged(synthetic_project):
    sid = synthetic_project.suite_id
    assert runner._record_run_stages("graded", sid, {"runId": "g-1"}, [_row(sid, True)], None) == []
    assert state.suite_state(sid)["baselined"]["runId"] == "g-1"
    assert runner._record_run_stages("graded", sid, {"runId": "g-2"}, [_row(sid, True)], 2) == []
    # subset run records the smoke marker, not the baseline
    assert state.suite_state(sid)["baselined"]["runId"] == "g-1"


def test_source_run_mode_reads_manifest(tmp_path):
    run_dir = tmp_path / "compare-20260815T000000Z"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"mode": "confirm"}))
    assert pairwise._source_run_mode(run_dir / "results.json") == "confirm"


def test_source_run_mode_falls_back_to_dir_name(tmp_path):
    run_dir = tmp_path / "confirm-20260815T000000Z"
    run_dir.mkdir()
    assert pairwise._source_run_mode(run_dir / "results.json") == "confirm"

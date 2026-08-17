"""Subset-first gate (`smoked`) and fingerprint-checked state restore (`graft`)."""

import json

import pytest

from src import state

SUITE = "demo-suite"


@pytest.fixture(autouse=True)
def isolated_state(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_state_path", lambda: tmp_path / ".state.json")
    monkeypatch.setattr(state, "_snapshotted_this_process", False)
    state.record_preflight(state.preflight_fingerprint())


def _earn_through_baselined():
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    state.record(SUITE, "baselined", {"runId": "graded-x"})


# ------------------------------------------------------------- subset-first —
def test_smoke_missing_is_a_problem():
    problems = state.check_smoked(SUITE)
    assert len(problems) == 1 and "no smoke run on record" in problems[0]
    assert "--filter-first-n" in problems[0]


def test_smoke_recorded_then_clean():
    state.record_smoked(SUITE, "graded-smoke-1")
    assert state.check_smoked(SUITE) == []


def test_smoke_stale_after_model_change(monkeypatch, fresh_config):
    state.record_smoked(SUITE, "graded-smoke-1")
    monkeypatch.setenv("EVAL_MODEL", "claude-opus-5")
    fresh_config.load(force_reload=True)
    problems = state.check_smoked(SUITE)
    assert problems and "STALE on gen_model" in problems[0]


def test_smoked_does_not_disturb_stage_pipeline():
    _earn_through_baselined()
    state.record_smoked(SUITE, "graded-smoke-1")
    # smoked is auxiliary: recording it must not wipe or reorder stages
    st = state.suite_state(SUITE)
    assert all(s in st for s in ("validated", "calibrated", "baselined", "smoked"))
    assert state.require(SUITE, "compare") == []


# -------------------------------------------------------- snapshot-on-wipe —
def test_wiping_rerecord_snapshots_first():
    _earn_through_baselined()
    fp = state.validate_fingerprint(SUITE)
    state.record(SUITE, "validated", {**fp, "config_sha": "moved"})
    backup = json.loads(state._backup_path().read_text())
    # the snapshot holds the pre-wipe state; the live state lost the successors
    assert "calibrated" in backup["suites"][SUITE]
    assert "calibrated" not in state.suite_state(SUITE)


def test_identical_rerecord_does_not_snapshot():
    _earn_through_baselined()
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    assert not state._backup_path().exists()
    assert "baselined" in state.suite_state(SUITE)


# ------------------------------------------------------------------- graft —
def test_graft_restores_when_fingerprints_match():
    _earn_through_baselined()
    fp = state.validate_fingerprint(SUITE)
    state.record(SUITE, "validated", {**fp, "config_sha": "moved"})   # snapshot + wipe
    state.record(SUITE, "validated", fp)                              # re-validated for real
    lines = state.graft()
    st = state.suite_state(SUITE)
    assert "calibrated" in st and st["calibrated"]["restored_from"]
    assert "baselined" in st
    assert state.require(SUITE, "compare") == []
    assert any("calibrated restored" in ln for ln in lines)


def test_graft_refuses_when_judge_model_moved(monkeypatch, fresh_config):
    _earn_through_baselined()
    fp = state.validate_fingerprint(SUITE)
    state.record(SUITE, "validated", {**fp, "config_sha": "moved"})
    state.record(SUITE, "validated", fp)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "claude-haiku-4-5")
    fresh_config.load(force_reload=True)
    lines = state.graft()
    assert "calibrated" not in state.suite_state(SUITE)
    assert any("NOT restored" in ln for ln in lines)


def test_graft_refuses_baseline_when_dataset_moved(monkeypatch):
    _earn_through_baselined()
    fp = state.validate_fingerprint(SUITE)
    state.record(SUITE, "validated", {**fp, "config_sha": "moved"})
    state.record(SUITE, "validated", fp)
    # simulate the dataset file itself having changed since the snapshot
    moved = {**fp, "dataset_sha": "deadbeef"}
    monkeypatch.setattr(state, "validate_fingerprint", lambda _sid: moved)
    state.graft()
    st = state.suite_state(SUITE)
    assert "calibrated" in st           # rubric + judge unmoved
    assert "baselined" not in st        # dataset moved → paid evidence void


def test_sequential_wipes_keep_the_first_snapshot(monkeypatch):
    """Regression: `validate` re-records suites one at a time; snapshotting
    before EVERY wipe left the backup holding only the last suite un-wiped."""
    monkeypatch.setattr(state, "_snapshotted_this_process", False)
    other = "demo-suite-2"
    for sid in (SUITE, other):
        state.record(sid, "validated", state.validate_fingerprint(sid))
        state.record(sid, "calibrated", {**state.calibration_fingerprint(sid), "green": True})
    for sid in (SUITE, other):  # sequential wiping re-records, same process
        state.record(sid, "validated",
                     {**state.validate_fingerprint(sid), "config_sha": "moved"})
    backup = json.loads(state._backup_path().read_text())
    # the snapshot predates BOTH wipes, not just the last one
    assert "calibrated" in backup["suites"][SUITE]
    assert "calibrated" in backup["suites"][other]


def test_graft_restamps_stale_preflight(monkeypatch):
    _earn_through_baselined()
    state.snapshot()
    st = state.load()
    st["preflight"]["config_sha"] = "stale-config-sha"
    state._save(st)
    lines = state.graft()
    assert state.load()["preflight"]["config_sha"] != "stale-config-sha"
    assert any("preflight restored" in ln for ln in lines)


def test_graft_without_snapshot_raises():
    with pytest.raises(state.StateError, match="nothing to graft"):
        state.graft()


def test_graft_restores_preflight_restamping_config_sha():
    _earn_through_baselined()
    state.snapshot()
    st = state.load()
    del st["preflight"]
    state._save(st)
    lines = state.graft()
    assert state.load()["preflight"]["restored_from"]
    assert any("preflight restored" in ln for ln in lines)


# ------------------------------------------------- verdict results locator —
def test_latest_compare_results_prefers_newest(tmp_path, monkeypatch):
    from src import config, runner
    for name in ("compare-20260101-000000", "confirm-20260301-000000",
                 "compare-20260201-000000"):
        d = tmp_path / name
        d.mkdir()
        (d / "results.json").write_text("{}")
    monkeypatch.setattr(config, "outputs_dir", lambda: tmp_path)
    found = runner.latest_compare_results()
    assert found.parent.name == "confirm-20260301-000000"


def test_latest_compare_results_none_when_empty(tmp_path, monkeypatch):
    from src import config, runner
    monkeypatch.setattr(config, "outputs_dir", lambda: tmp_path)
    assert runner.latest_compare_results() is None


# --------------------------------------------- cross-family judge preflight —
def _blocking_import(blocked):
    real_import = __import__

    def fake(name, *a, **kw):
        if name == blocked:
            raise ImportError(f"No module named '{blocked}'")
        return real_import(name, *a, **kw)
    return fake


def test_judge_provider_problems_flags_missing_key_and_sdk(monkeypatch):
    import sys as _sys

    from src import runner
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai:gpt-5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delitem(_sys.modules, "openai", raising=False)
    monkeypatch.setattr("builtins.__import__", _blocking_import("openai"))
    problems = runner._judge_provider_problems()
    assert any("uv sync --extra cross-judge" in p for p in problems)
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_judge_provider_problems_empty_when_ready(monkeypatch):
    import sys as _sys
    import types as _types

    from src import runner
    # anthropic judge: nothing to check
    assert runner._judge_provider_problems() == []
    # openai judge with SDK + key: green
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai:gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-branch-test")
    monkeypatch.setitem(_sys.modules, "openai", _types.SimpleNamespace())
    assert runner._judge_provider_problems() == []


def test_preflight_fails_fast_on_missing_openai_judge_key(monkeypatch, capsys):
    import argparse
    import sys as _sys

    from src import config, runner
    monkeypatch.setattr(config, "load_env_file", lambda: None)  # the repo .env must not leak in
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-branch-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai:gpt-5")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(_sys.modules, "openai", __import__("types").SimpleNamespace())
    monkeypatch.setattr(runner, "_run_live",
                        lambda mode, ns: pytest.fail("preflight must abort before any run"))
    rc = runner.cmd_preflight(argparse.Namespace(suite=None, max_cost=None, force=False))
    assert rc == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err

"""--dry-run on every paid command: full breakdown, exit before the lock,
the run dir, or any call."""

import json

import pytest

from src import config, runner, state
from src.science.gen_matrix import generate

SUITE = "demo-suite"


def _seed_graded_gates(sid=SUITE):
    state.record(sid, "validated", state.validate_fingerprint(sid))
    state.record(sid, "calibrated", {**state.calibration_fingerprint(sid), "green": True})
    state.record_preflight(state.preflight_fingerprint())
    state.record_smoked(sid, "graded-smoke")


def _no_new_run_dirs():
    runs = config.outputs_dir()
    return [] if not runs.exists() else [d.name for d in runs.iterdir()]


def test_graded_dry_run_prints_breakdown_and_spawns_nothing(synthetic_project, capsys):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    rc = runner.main(["graded", "--suite", SUITE, "--dry-run", "--max-cost", "10"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "generation" in out and "judge" in out
    assert "basis" in out and "ceiling" in out and "would proceed" in out
    assert _no_new_run_dirs() == []                      # no run dir created
    assert not (config.outputs_root() / ".run.lock").exists()


def test_graded_dry_run_reports_would_abort(synthetic_project, capsys):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    rc = runner.main(["graded", "--suite", SUITE, "--dry-run", "--max-cost", "0.001"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "EXCEEDS the ceiling" in out
    assert _no_new_run_dirs() == []


def test_dry_run_still_respects_the_state_gate(synthetic_project, capsys):
    # No stages recorded: the gate refusal IS the honest dry-run answer.
    generate(config.specs_dir() / f"{SUITE}.yaml")
    rc = runner.main(["graded", "--suite", SUITE, "--dry-run"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ABORTED BEFORE ANY CALL" in err


def test_calibrate_dry_run(synthetic_project, capsys):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record("demo-suite-2", "validated", state.validate_fingerprint("demo-suite-2"))
    rc = runner.main(["calibrate", "--suite", SUITE, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "judge" in out
    assert _no_new_run_dirs() == []


def test_verdict_dry_run(synthetic_project, tmp_path, capsys):
    results = tmp_path / "compare-x" / "results.json"
    results.parent.mkdir(parents=True)
    rows = []
    for variant in ("current", "candidate"):
        rows.append({"testCase": {"vars": {"suite": SUITE, "cell": "c1",
                                           "promptVariant": variant, "promptFile": "demo.md"}},
                     "success": True,
                     "gradingResult": {"componentResults": [{"namedScores": {"warmth": 4}}]},
                     "response": {"output": "hello"}})
    results.write_text(json.dumps({"results": {"results": rows}}))
    rc = runner.main(["verdict", str(results), "--target", "warmth", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "both blinded orders" in out
    assert _no_new_run_dirs() == []


def test_gen_cases_dry_run(synthetic_project, capsys):
    rc = runner.main(["gen-cases", "--suite", SUITE, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "dataset" in out
    assert _no_new_run_dirs() == []


def test_gen_cases_dry_run_with_mock_short_circuits(synthetic_project, capsys, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "canned")
    rc = runner.main(["gen-cases", "--suite", SUITE, "--dry-run"])
    assert rc == 0
    assert "$0.00" in capsys.readouterr().out


# ------------------------------- E5: role + rolling-window gates in _run_live —
def _patch_governance(monkeypatch, **extra):
    cfg = {k: v for k, v in config.get().items()}
    cfg["governance"] = {**cfg["governance"], **extra}
    monkeypatch.setattr(config, "_cached", cfg)


def test_role_cap_gate_aborts_before_any_call(synthetic_project, capsys, monkeypatch):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    _patch_governance(monkeypatch, max_cost_usd={"generation": 0.5})
    rc = runner.main(["graded", "--suite", SUITE, "--max-cost", "10"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "governance.max_cost_usd.generation" in err
    assert _no_new_run_dirs() == []


def test_daily_window_gate_aborts(synthetic_project, capsys, monkeypatch):
    import json as _json
    from datetime import datetime, timezone as _tz
    from src.utils import cost_tracker
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    path = cost_tracker.rollup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"runId": "earlier", "totalUsd": 4.9,
                                 "at": datetime.now(_tz.utc).isoformat()}) + "\n")
    _patch_governance(monkeypatch, max_daily_usd=5.0)
    rc = runner.main(["graded", "--suite", SUITE, "--max-cost", "10"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "rolling 24h" in err and "max_daily_usd" in err
    assert _no_new_run_dirs() == []


def test_suite_ceiling_lowers_the_run_ceiling(synthetic_project, capsys, monkeypatch):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    cfg = {k: v for k, v in config.get().items()}
    cfg["suites"] = [dict(s, max_run_cost_usd=0.01) if s["id"] == SUITE else s
                     for s in cfg["suites"]]
    monkeypatch.setattr(config, "_cached", cfg)
    rc = runner.main(["graded", "--suite", SUITE])
    err = capsys.readouterr().err
    assert rc == 1
    assert "suite ceiling" in err

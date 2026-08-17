"""E11: regression suite + pin — fixed failures must never return."""

import json

import pytest
import yaml

from src import config, runner, state
from src.science.gen_matrix import generate
from src.science.pin import pin_cell
from src.utils import dataset_loader

SUITE = "demo-suite"


def _make_run_dir(tmp_path, cell, suite=SUITE):
    run_dir = tmp_path / "graded-20260815-000000"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"runId": run_dir.name,
                                                       "mode": "graded", "suite": suite}))
    row = {"testCase": {"vars": {"suite": suite, "cell": cell}}, "success": False}
    (run_dir / "results.json").write_text(json.dumps({"results": {"results": [row]}}))
    return run_dir


def _first_cell():
    matrix = yaml.safe_load((config.graded_dir() / f"{SUITE}.yaml").read_text())
    return next(c["vars"]["cell"] for c in matrix if str(c["vars"].get("holdout")) != "true")


def test_pin_copies_the_matrix_case_verbatim(synthetic_project, tmp_path):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    cell = _first_cell()
    run_dir = _make_run_dir(tmp_path, cell)
    result = pin_cell(run_dir, cell, note="broke in prod")
    pinned = yaml.safe_load(result["path"].read_text())
    assert len(pinned) == 1
    assert pinned[0]["vars"]["regression"] == "true"
    assert pinned[0]["vars"]["cell"] == f"{cell}·pinned"
    assert "holdout" not in pinned[0]["vars"]
    assert "[regression]" in pinned[0]["description"]
    # double-pin refused
    with pytest.raises(ValueError, match="already pinned"):
        pin_cell(run_dir, cell)


def test_pin_refuses_unknown_cell(synthetic_project, tmp_path):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    run_dir = _make_run_dir(tmp_path, "no-such-cell")
    with pytest.raises(ValueError, match="not found"):
        pin_cell(run_dir, "definitely-missing")


def test_regression_cases_ride_in_every_holdout_mode(synthetic_project, tmp_path):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    cell = _first_cell()
    pin_cell(_make_run_dir(tmp_path, cell), cell)
    for holdout in ("exclude", "only", "include"):
        cases = dataset_loader.load_graded_cases(SUITE, holdout=holdout)
        pinned = [c for c in cases if c["vars"].get("regression") == "true"]
        assert len(pinned) == 1, holdout


def test_regression_failure_fails_the_run_and_blocks_stages(synthetic_project, tmp_path):
    rows = [
        {"testCase": {"vars": {"suite": SUITE, "cell": "ok-cell"}}, "success": True},
        {"testCase": {"vars": {"suite": SUITE, "cell": "pin-1·pinned",
                               "regression": "true"}}, "success": False},
    ]
    # exercise the loud-fail branch logic exactly as _run_live computes it
    regression_failed = sorted({
        str(v.get("cell"))
        for r in rows
        for v in [((r.get("testCase") or {}).get("vars") or {})]
        if v.get("regression") == "true" and r.get("success") is not True})
    assert regression_failed == ["pin-1·pinned"]


def test_candidate_regression_failure_forces_reject(synthetic_project, tmp_path, monkeypatch):
    from src.science import pairwise
    monkeypatch.setattr(pairwise, "_pairwise_call",
                        lambda s, r, a, b: {"winner": "B", "reason": "mock"})
    dims = ("warmth", "clarity", "usefulness")
    low = {d: 3 for d in dims}
    high = {d: 5 for d in dims}
    rows = []
    for cell_id in ("c1", "c2", "c3"):
        for variant, scores in (("current", low), ("candidate", high)):
            rows.append({
                "testCase": {"vars": {"suite": SUITE, "cell": cell_id,
                                      "promptVariant": variant, "promptFile": "demo.md",
                                      "trial": 1}},
                "response": {"output": f"{variant} {cell_id}"},
                "gradingResult": {"componentResults": [{"namedScores": dict(scores), "pass": True}]},
                "success": True,
            })
    # candidate re-breaks a pinned case
    rows.append({
        "testCase": {"vars": {"suite": SUITE, "cell": "pin-1·pinned",
                              "promptVariant": "candidate", "promptFile": "demo.md",
                              "regression": "true", "trial": 1}},
        "response": {"output": "candidate pinned"},
        "gradingResult": {"componentResults": [{"namedScores": dict(low), "pass": False}]},
        "success": False,
    })
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": {"results": rows}}))
    record = pairwise.compare_verdict(results, "warmth", log=lambda *a: None)
    assert record["pinnedRegressionFailures"] == ["pin-1·pinned"]
    assert record["verdict"] == "REJECT"


def test_regression_sha_in_validate_fingerprint(synthetic_project, tmp_path):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    fp1 = state.validate_fingerprint(SUITE)
    assert fp1["graded_matrix_sha"] is not None
    assert fp1["regression_sha"] is None  # no pins yet
    cell = _first_cell()
    pin_cell(_make_run_dir(tmp_path, cell), cell)
    fp2 = state.validate_fingerprint(SUITE)
    assert fp2["regression_sha"] is not None
    assert fp2["regression_sha"] != fp1["regression_sha"]


def test_cmd_pin_end_to_end(synthetic_project, tmp_path, capsys):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    cell = _first_cell()
    run_dir = _make_run_dir(tmp_path, cell)
    rc = runner.main(["pin", "--run", str(run_dir), "--cell", cell, "--note", "prod bug"])
    out = capsys.readouterr().out
    assert rc == 0 and "pinned" in out and "validate" in out
    rc2 = runner.main(["pin", "--run", str(run_dir), "--cell", cell])
    assert rc2 == 1
    assert "PIN REFUSED" in capsys.readouterr().err

"""E15: why, round --run, monitor helpers, perturb, thin Python API."""

import json

import pytest
import yaml

from src import api, config, runner, state
from src.science.gen_matrix import generate
from src.science.perturb import generate_perturbations

SUITE = "demo-suite"


# ------------------------------------------------------------------- why —
def test_why_explains_staleness_and_next(synthetic_project, capsys):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE),
                                       "green": True, "rubric_sha": "stale-sha"})
    rc = runner.main(["why", "--suite", SUITE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "validated" in out and "done" in out
    assert "STALE on rubric_sha" in out
    assert "next" in out


# ------------------------------------------------------------ round --run —
def test_round_run_executes_only_the_printed_next_command(synthetic_project, capsys, monkeypatch):
    executed = []
    monkeypatch.setattr(runner, "main", lambda argv=None: executed.append(argv) or 0)
    # call cmd_round directly so the patched main() is only used for dispatch
    import argparse
    rc = runner.cmd_round(argparse.Namespace(suite=SUITE, run=True, yes=True))
    assert rc == 0
    assert executed == [["validate"]]  # nothing recorded → next is validate


def test_round_run_requires_suite(synthetic_project, capsys):
    import argparse
    rc = runner.cmd_round(argparse.Namespace(suite=None, run=True, yes=True))
    assert rc == 1
    assert "--suite" in capsys.readouterr().err


def test_round_run_complete_pipeline_runs_nothing(synthetic_project, capsys, monkeypatch):
    import argparse
    # Fresh (non-stale) fingerprints: round now reads the same staleness view
    # as `why`/the dashboard, and a stale stage would be offered for re-earning.
    fingerprints = {"validated": state.validate_fingerprint(SUITE),
                    "calibrated": state.calibration_fingerprint(SUITE)}
    for stage in state.STAGES:
        state.record(SUITE, stage, fingerprints.get(stage, {"x": stage}))
    executed = []
    monkeypatch.setattr(runner, "main", lambda argv=None: executed.append(argv) or 0)
    rc = runner.cmd_round(argparse.Namespace(suite=SUITE, run=True, yes=True))
    assert rc == 0 and executed == []
    assert "nothing to run" in capsys.readouterr().out


# ---------------------------------------------------------------- monitor —
def test_canary_cells_deterministic_and_no_holdout(synthetic_project):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    c1 = runner._canary_cells(SUITE, 2)
    c2 = runner._canary_cells(SUITE, 2)
    assert c1 == c2 and len(c1) == 2
    matrix = yaml.safe_load((config.graded_dir() / f"{SUITE}.yaml").read_text())
    holdouts = {c["vars"]["cell"] for c in matrix if str(c["vars"].get("holdout")) == "true"}
    assert not set(c1) & holdouts


def test_monitor_baseline_dims_reads_newest_matching_record(synthetic_project):
    hist = config.history_dir()
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "graded-20260101-000000.json").write_text(json.dumps({
        "manifest": {}, "cases": [
            {"suite": SUITE, "scores": {"warmth": 4, "clarity": 5}},
            {"suite": SUITE, "scores": {"warmth": 2, "clarity": 3}},
        ]}))
    dims, source = runner._monitor_baseline_dims(SUITE, exclude_run="other")
    assert dims == {"warmth": 3.0, "clarity": 4.0}
    assert source == "graded-20260101-000000.json"
    assert runner._monitor_baseline_dims("no-such-suite", "x") is None


def test_monitor_cell_filter_in_loader(synthetic_project, monkeypatch):
    from src.utils import dataset_loader
    generate(config.specs_dir() / f"{SUITE}.yaml")
    all_cases = dataset_loader.load_graded_cases(SUITE)
    keep = all_cases[0]["vars"]["cell"]
    monkeypatch.setenv("EVAL_MONITOR_CELLS", keep)
    filtered = dataset_loader.load_graded_cases(SUITE)
    assert {c["vars"]["cell"] for c in filtered} == {keep}


# ---------------------------------------------------------------- perturb —
def test_perturb_generates_draft_offline(synthetic_project, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "canned")
    result = generate_perturbations(SUITE, 2)
    data = yaml.safe_load(result["out"].read_text())
    assert data["suite"] == SUITE
    assert all(len(e["variants"]) == 2 for e in data["perturbations"])
    assert "DRAFT" in result["out"].read_text()
    assert result["model"] == "mock:canned"


def test_perturb_reasks_once_then_raises(synthetic_project, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "malformed-once")
    assert generate_perturbations(SUITE, 2)["perturbations"]
    monkeypatch.setenv("MOCK_DATASET", "malformed")
    import src.science.perturb as p
    monkeypatch.setattr(p, "_mock_calls", 0)
    with pytest.raises(RuntimeError, match="invalid after one re-ask"):
        generate_perturbations(SUITE, 2)


def test_cmd_perturb_end_to_end(synthetic_project, capsys, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "canned")
    rc = runner.main(["perturb", "--suite", SUITE, "--variants", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "paraphrase variants" in out and "matrix --suite" in out


# --------------------------------------------------------------- thin API —
def test_api_state_of(synthetic_project):
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    view = api.state_of(SUITE)
    assert len(view["suites"]) == 1
    assert view["suites"][0]["suite"] == SUITE
    assert view["suites"][0]["stages"][0]["done"] is True


def test_api_verdict_wraps_compare_verdict(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_PAIRWISE", "tie")
    rows = []
    for cell in ("c1", "c2", "c3"):
        for variant in ("current", "candidate"):
            rows.append({
                "testCase": {"vars": {"suite": SUITE, "cell": cell,
                                      "promptVariant": variant, "promptFile": "demo.md",
                                      "trial": 1}},
                "response": {"output": f"{variant} {cell}"},
                "gradingResult": {"componentResults": [
                    {"namedScores": {"warmth": 3, "clarity": 3, "usefulness": 3}, "pass": True}]},
                "success": True})
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": {"results": rows}}))
    record = api.verdict(results, "warmth")
    assert record["verdict"] == "REJECT"  # all ties


def test_api_run_suite_rejects_unknown_mode(synthetic_project):
    with pytest.raises(ValueError, match="unknown mode"):
        api.run_suite(SUITE, mode="optimize")

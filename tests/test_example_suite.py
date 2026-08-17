"""The shipped support-reply example — adopted through the real bundle path
into a throwaway project, its goldens proven against its contracts, and its
band-mock calibration green. Mirrors the CI example-smoke job without
touching this repo's own bare config."""

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, runner, state
from src.reports import checks

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "examples" / "support-reply" / "bundle"
SUITE = "support-reply"


def _ns(**kw):
    base = dict(path=None, as_kind=None, suite=None, yes=True, dry_run=False,
                print_config=False, rubric=None, example=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def bare_project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    for d in ("prompts/production", "prompts/candidates", "datasets/graded-specs",
              "datasets/graded", "judges", "fixtures/golden", "outputs", "input"):
        (root / d).mkdir(parents=True)
    cfg = f"""version: 2

project:
  name: example-smoke
  production_model: claude-sonnet-5
  # asserts_file: project/asserts.py

paths:
  fixtures_dir: {root}/fixtures
  graded_dir: {root}/datasets/graded
  outputs_dir: {root}/outputs

prompts:
  production_dir: {root}/prompts/production
  candidates_dir: {root}/prompts/candidates

suites:
  []

agents:
  generation:
    model: claude-sonnet-5
    max_budget_usd: 3
    max_tokens_per_turn: 64000
  judge:
    model: claude-sonnet-4-6
    max_tokens_per_turn: 24000
  dataset:
    model: claude-haiku-4-5

pricing:
  claude-sonnet-5: {{ input: 3, output: 15 }}

governance:
  max_run_cost_usd: 1.0

graded:
  specs_dir: {root}/datasets/graded-specs
  repeat: 3
  max_cells_per_suite: 8
  holdout: 0.25
  calibration:
    samples: 3
    pass_min: 4
    fail_max: 2

observability:
  runs_dir: {root}/outputs/runs
  history_dir: {root}/outputs/history
"""
    config_path = root / "eval_config.yaml"
    config_path.write_text(cfg)
    monkeypatch.setenv("EVAL_CONFIG", str(config_path))
    config.load(force_reload=True)
    yield SimpleNamespace(root=root)
    monkeypatch.delenv("EVAL_CONFIG")
    config.load(force_reload=True)


# ---------------------------------------------- contracts vs the goldens —
def _contracts():
    spec = importlib.util.spec_from_file_location("example_contracts", BUNDLE / "contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [module.acknowledges_customer, module.has_next_step,
            module.no_internal_leak, module.no_unfilled_template]


def test_pass_goldens_clear_every_contract():
    for golden in sorted((BUNDLE / SUITE).glob("pass*.txt")):
        text = golden.read_text()
        for contract in _contracts():
            result = contract(text, {})
            assert result["pass"], (golden.name, result["reason"])


def test_fail_goldens_each_trip_a_contract():
    for golden in sorted((BUNDLE / SUITE).glob("fail*.txt")):
        text = golden.read_text()
        tripped = [c.__name__ for c in _contracts() if not c(text, {})["pass"]]
        assert tripped, f"{golden.name} trips no contract — validate could not catch it"


def test_bundle_ships_three_of_each():
    assert len(list((BUNDLE / SUITE).glob("pass*.txt"))) == 3
    assert len(list((BUNDLE / SUITE).glob("fail*.txt"))) == 3


# ------------------------------------------------------------- adoption —
def test_example_adopts_cleanly_and_checks_green(bare_project, capsys):
    assert runner.cmd_init(_ns(example=SUITE)) == 0
    out = capsys.readouterr().out
    assert "staged examples/support-reply/bundle" in out
    config.load(force_reload=True)
    assert config.suite_by_id(SUITE)["rubric"]
    errors, warnings = checks.run_checks()
    assert errors == []
    # real content, not scaffold: nothing placeholder-flagged
    assert not any("scaffolded" in w for w in warnings)
    # the graded matrix was generated during adoption
    assert (bare_project.root / "datasets/graded" / f"{SUITE}.yaml").exists()


def test_band_calibration_green_on_example(bare_project, monkeypatch, capsys):
    assert runner.cmd_init(_ns(example=SUITE)) == 0
    config.load(force_reload=True)
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "band")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    rc = runner.main(["calibrate", "--suite", SUITE])
    out = capsys.readouterr().out
    assert rc == 0 and "CALIBRATION GREEN" in out
    assert state.suite_state(SUITE)["calibrated"]["judge_model"] == "mock:band"


def test_unknown_example_is_refused_with_listing(bare_project, capsys):
    assert runner.cmd_init(_ns(example="nope")) == 1
    err = capsys.readouterr().err
    assert "support-reply" in err          # the available list names the real one


def test_already_staged_example_is_refused(bare_project, capsys):
    (bare_project.root / "input" / SUITE).mkdir()
    assert runner.cmd_init(_ns(example=SUITE)) == 1
    assert "already exists" in capsys.readouterr().err

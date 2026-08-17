"""E16 — per-suite http provider: config validation, single-suite refusal,
the materialized per-run promptfoo config, cost_unknown metering, and the R4
rule that unknown-cost rows sum to $0 everywhere money is counted. Zero live
HTTP anywhere: promptfoo is monkeypatched, hooks are called directly."""

import json

import pytest
import yaml

from src import config, runner, state
from src.science.gen_matrix import generate
from src.utils import cost_tracker, proc

SUITE = "demo-suite"

PROVIDER = {"type": "http", "url": "https://api.example.test/generate",
            "method": "POST", "headers": {"x-api-key": "sekrit"},
            "body": {"prompt": "{{prompt}}"}}


def _set_provider(project, provider):
    path = project.root / "eval_config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["suites"][0]["provider"] = provider
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    config.load(force_reload=True)


def _seed_graded_gates(sid=SUITE):
    state.record(sid, "validated", state.validate_fingerprint(sid))
    state.record(sid, "calibrated", {**state.calibration_fingerprint(sid), "green": True})
    state.record_preflight(state.preflight_fingerprint())
    state.record_smoked(sid, "graded-smoke")


# ------------------------------------------------------ config validation —
@pytest.mark.parametrize("provider", [
    "https://api.example.test",                      # not a mapping
    {"type": "openai", "url": "https://x.test"},     # unsupported type
    {"type": "http"},                                # url missing
    {"type": "http", "url": "ftp://x.test"},         # not http(s)
])
def test_bad_provider_declarations_fail_config_load(synthetic_project, provider):
    with pytest.raises(config.ConfigError):
        _set_provider(synthetic_project, provider)


def test_valid_provider_declaration_loads(synthetic_project):
    _set_provider(synthetic_project, PROVIDER)
    assert config.suite_by_id(SUITE)["provider"]["url"] == PROVIDER["url"]


# ------------------------------------------------- single-suite refusal —
def test_http_run_without_suite_is_refused_before_any_gate(synthetic_project, capsys):
    _set_provider(synthetic_project, PROVIDER)
    rc = runner.main(["graded"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "http" in err and "--suite" in err
    runs = config.outputs_dir()
    assert not runs.exists() or list(runs.iterdir()) == []


# ------------------------------------------------- materialized config —
def test_materialized_config_shape(synthetic_project, tmp_path):
    out = runner._materialize_http_config(PROVIDER, "graded", tmp_path)
    doc = yaml.safe_load(out.read_text())
    assert out.name == "promptfooconfig.http.yaml"
    prov = doc["providers"][0]
    assert prov["id"] == PROVIDER["url"]
    assert prov["config"] == {"method": "POST", "headers": {"x-api-key": "sekrit"},
                              "body": {"prompt": "{{prompt}}"}}
    assert doc["tests"].endswith("src/loaders/pf_tests.py:generate_graded_tests")
    assert doc["prompts"][0]["id"].endswith("src/loaders/pf_prompts.py:current_prompt")
    assert doc["prompts"][0]["id"].startswith("file:///")  # absolute — resolved from run dir
    assert doc["extensions"][0].endswith("src/hooks.py:extension_hook")


def test_materialized_compare_config_uses_compare_prompt(synthetic_project, tmp_path):
    doc = yaml.safe_load(runner._materialize_http_config(PROVIDER, "compare", tmp_path).read_text())
    assert doc["prompts"][0]["id"].endswith(":compare_prompt")
    assert doc["tests"].endswith(":generate_compare_graded_tests")


# ----------------------------------------------------------- dry run —
def test_http_dry_run_prices_judge_only(synthetic_project, capsys):
    _set_provider(synthetic_project, PROVIDER)
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    rc = runner.main(["graded", "--suite", SUITE, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HTTP PROVIDER" in out and "JUDGE spend only" in out
    # generation priced at 0 — the total must equal the judge line alone
    gen_line = next(line for line in out.splitlines() if line.strip().startswith("generation"))
    assert "$0.0000" in gen_line


# ------------------------------------------------------------ full run —
def test_http_graded_run_end_to_end(synthetic_project, monkeypatch, capsys):
    _set_provider(synthetic_project, PROVIDER)
    generate(config.specs_dir() / f"{SUITE}.yaml")
    _seed_graded_gates()
    seen = {}

    def fake_run_promptfoo(pf_args, env, run_dir, ceiling, breaker, **kw):
        seen["env"] = env
        seen["config_path"] = pf_args[pf_args.index("-c") + 1]
        rows = [{"testCase": {"vars": {"suite": SUITE, "cell": "c1"}}, "success": True,
                 "gradingResult": {"componentResults": [{"namedScores": {"warmth": 4}}]},
                 "response": {"output": "hello"}}]
        out_file = pf_args[pf_args.index("-o") + 1]
        with open(out_file, "w") as f:
            json.dump({"results": {"results": rows}}, f)
        return proc.RunOutcome(returncode=0)

    monkeypatch.setattr(proc, "run_promptfoo", fake_run_promptfoo)
    rc = runner.main(["graded", "--suite", SUITE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "HTTP PROVIDER" in out
    assert seen["env"]["EVAL_HTTP_PROVIDER"] == "1"

    run_dir = max(config.outputs_dir().iterdir())
    assert seen["config_path"] == str(run_dir / "promptfooconfig.http.yaml")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["transport"] == "http"
    assert manifest["httpProvider"]["url"] == PROVIDER["url"]
    assert manifest["httpProvider"]["headerKeys"] == ["x-api-key"]
    assert "headers" not in manifest["httpProvider"]           # values never durable
    assert "sekrit" not in (run_dir / "manifest.json").read_text()
    kinds = {e["kind"] for e in cost_tracker.read_ledger(run_dir)}
    assert "alert" in kinds                                     # the loud judge-only alert
    assert state.suite_state(SUITE)["baselined"]["runId"] == manifest["runId"]


# --------------------------------------------- hooks: cost_unknown rows —
def test_hook_records_cost_unknown_for_uncosted_http_rows(synthetic_project, monkeypatch, tmp_path):
    from src import hooks
    monkeypatch.setenv("EVAL_HTTP_PROVIDER", "1")
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    hooks._after_each({"result": {"response": {"output": "text"}, "success": True},
                       "test": {"vars": {"cell": "c1"}}})
    entries = cost_tracker.read_ledger(tmp_path)
    unknown = [e for e in entries if e["kind"] == "cost_unknown"]
    assert len(unknown) == 1 and unknown[0]["usd"] == 0.0
    assert unknown[0]["case"] == "c1"


def test_hook_skips_cost_unknown_when_cached_or_costed(synthetic_project, monkeypatch, tmp_path):
    from src import hooks
    monkeypatch.setenv("EVAL_HTTP_PROVIDER", "1")
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    hooks._after_each({"result": {"response": {"output": "t", "cached": True}},
                       "test": {"vars": {"cell": "c1"}}})
    hooks._after_each({"result": {"response": {"output": "t", "cost": 0.02}},
                       "test": {"vars": {"cell": "c2"}}})
    kinds = [e["kind"] for e in cost_tracker.read_ledger(tmp_path)]
    assert "cost_unknown" not in kinds


# --------------------------------------------------- R4: $0 everywhere —
def test_cost_unknown_sums_to_zero_and_never_rolls_up(synthetic_project, tmp_path):
    rdir = config.outputs_dir() / "graded-http-x"
    rdir.mkdir(parents=True)
    (rdir / "spend.jsonl").write_text(
        json.dumps({"kind": "cost_unknown", "usd": 0.0, "at": "2026-08-16T00:00:00+00:00"}) + "\n"
        + json.dumps({"kind": "alert", "usd": 0.0, "at": "2026-08-16T00:00:00+00:00"}) + "\n")
    assert cost_tracker.summary_for_run(rdir)["totalUsd"] == 0.0
    assert cost_tracker.append_rollup(rdir, "graded-http-x", "graded", SUITE) is None
    assert not cost_tracker.rollup_path().exists()
    assert cost_tracker.window_spend(24) == 0.0


def test_measured_estimates_ignores_cost_unknown_records(synthetic_project):
    hist = config.history_dir()
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "graded-20990101-000000.json").write_text(json.dumps({
        "manifest": {"mode": "graded",
                     "spend": {"calls": 3, "byKind": {"cost_unknown": {"calls": 3, "usd": 0.0}}}},
        "cases": [{"suite": SUITE}],
    }))
    assert cost_tracker.measured_estimates(SUITE) is None


# --------------------------------------- generator: no workspace/options —
def test_expand_skips_workspace_and_options_in_http_mode(monkeypatch):
    from src.loaders import pf_tests
    monkeypatch.setenv("EVAL_HTTP_PROVIDER", "1")
    # No EVAL_RUN_DIR on purpose: a workspace attempt would raise RuntimeError.
    case = {"description": "d", "vars": {"suite": SUITE, "cell": "c", "probe": "p"}}
    out = pf_tests._expand([case], 2, None)
    assert len(out) == 2
    for i, t in enumerate(out, start=1):
        assert t["vars"]["trial"] == str(i)
        assert "workdir" not in t["vars"]
        assert "options" not in t


# ------------------------------------------------ assert-ref absolutizing —
def test_http_mode_absolutizes_relative_assert_refs(synthetic_project, monkeypatch):
    """The materialized http config lives in the RUN DIR and promptfoo
    resolves relative file:// assert refs against the config's directory —
    without absolutizing, every contract errors FileNotFound (found live in
    the pre-launch simulation)."""
    from src.loaders import pf_tests
    monkeypatch.setenv("EVAL_HTTP_PROVIDER", "1")
    case = {"description": "c", "vars": {"suite": SUITE, "promptFile": "demo.md"},
            "assert": [{"type": "python", "value": "file://project/contracts.py:fn"},
                       {"type": "python", "value": f"file:///{'abs'}/x.py:fn"}]}
    out = pf_tests._expand([case], 1, None)
    values = [a["value"] for a in out[0]["assert"]]
    assert values[0] == f"file://{config.ROOT}/project/contracts.py:fn"
    assert values[1] == "file:///abs/x.py:fn"  # already absolute — untouched

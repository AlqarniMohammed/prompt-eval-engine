import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests must never inherit run-scoped env from the invoking shell."""
    for var in ("EVAL_RUN_DIR", "EVAL_SUITE", "EVAL_MODEL", "EVAL_JUDGE_MODEL",
                "EVAL_REPEAT", "EVAL_HOLDOUT", "EVAL_OFFLINE", "EVAL_CONFIG",
                "EVAL_ADAPTIVE_K", "EVAL_HTTP_PROVIDER", "EVAL_MONITOR_CELLS",
                "MOCK_GRADED_JUDGE", "MOCK_JUDGE",
                "MOCK_PAIRWISE", "MOCK_DATASET", "GOLDEN_SET", "GOLDEN_VARIANT"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def fresh_config():
    """Force a config reload after env changes."""
    from src import config
    config.load(force_reload=True)
    yield config
    config.load(force_reload=True)


PRICING = {
    "claude-fable-5": {"input": 10, "output": 50, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25},
}


@pytest.fixture
def synthetic_project(tmp_path, monkeypatch):
    """A complete throwaway project the engine runs against — the repo ships
    no example content, so suite-dependent tests build their own: one prompt,
    one dataset case, an asserts module, a 3-dimension rubric, golden
    pass/fail bundles, and a graded spec. EVAL_CONFIG points config at it."""
    from src import config

    root = tmp_path / "proj"
    for d in ("prompts/production", "prompts/candidates", "datasets/graded-specs",
              "datasets/graded", "judges", "fixtures/golden/demo",
              "fixtures/workspace", "outputs"):
        (root / d).mkdir(parents=True)

    (root / "asserts.py").write_text(
        "def demo_greets(output, context):\n"
        '    ok = "hello" in str(output).lower()\n'
        '    return {"pass": ok, "score": 1.0 if ok else 0.0,\n'
        '            "reason": "greeting present" if ok else "no greeting found"}\n'
    )
    (root / "prompts/production/demo.md").write_text(
        "Greet the user warmly, then answer their question.")
    (root / "fixtures/workspace/notes.md").write_text("workspace fixture file\n")
    (root / "fixtures/golden/demo/pass.txt").write_text(
        "===== STDOUT =====\nHello there! Happy to help with that.\n")
    (root / "fixtures/golden/demo/fail.txt").write_text(
        "===== STDOUT =====\nThe answer is 42.\n")

    rubric = "# Rubric: demo-suite\nGrade one greeting reply.\n"
    for dim in ("warmth", "clarity", "usefulness"):
        rubric += (f"## dimension: {dim}\nWhat it measures: {dim} of the reply\n"
                   + "\n".join(f"- {n}: {dim} level {n}" for n in range(5, 0, -1)) + "\n")
    (root / "judges/demo.md").write_text(rubric)

    asserts_ref = f"file://{root / 'asserts.py'}:demo_greets"
    (root / "datasets/demo-suite.yaml").write_text(yaml.safe_dump([{
        "description": "demo — user says hi, reply must greet",
        "vars": {"promptFile": "demo.md", "fixture": "workspace",
                 "golden": "demo", "probe": "hi, quick question about my order"},
        "assert": [{"type": "python", "value": asserts_ref}],
    }], sort_keys=False))

    (root / "datasets/graded-specs/demo-suite.yaml").write_text(yaml.safe_dump({
        "suite": "demo-suite",
        "base": {"promptFile": "demo.md", "fixture": "workspace"},
        "contracts": [{"type": "python", "value": asserts_ref}],
        "axes": {
            "tone": [
                {"id": "polite", "probe": "hello, could you help me when you have a moment?"},
                {"id": "rushed", "probe": "need this now, no time"},
            ],
            "detail": [
                {"id": "vague", "vars": {"golden": "demo"}, "probe": "something's wrong with it"},
                {"id": "specific", "vars": {"golden": "demo"}, "probe": "order 118 arrived damaged"},
            ],
        },
    }, sort_keys=False))

    suites = [
        {"id": "demo-suite", "file": str(root / "datasets/demo-suite.yaml"),
         "rubric": str(root / "judges/demo.md")},
        {"id": "demo-suite-2", "file": str(root / "datasets/demo-suite.yaml"),
         "rubric": str(root / "judges/demo.md")},
    ]
    cfg = {
        "version": 2,
        "project": {"name": "synthetic", "production_model": "claude-sonnet-5",
                    "asserts_file": str(root / "asserts.py")},
        "paths": {"fixtures_dir": str(root / "fixtures"),
                  "graded_dir": str(root / "datasets/graded"),
                  "outputs_dir": str(root / "outputs")},
        "prompts": {"production_dir": str(root / "prompts/production"),
                    "candidates_dir": str(root / "prompts/candidates")},
        "suites": suites,
        "agents": {
            "generation": {"model": "claude-sonnet-5", "max_budget_usd": 3,
                           "max_turns": 40, "max_tokens_per_turn": 64000,
                           "max_concurrency": 2, "repeat": 1},
            "judge": {"model": "claude-sonnet-4-6", "temperature": 0, "max_budget_usd": 1,
                      "max_tokens_per_turn": 24000, "max_output_tokens_target": 2000},
            "dataset": {"model": "claude-haiku-4-5", "max_tokens_per_turn": 8000,
                        "max_budget_usd": 0.5},
        },
        "pricing": PRICING,
        "governance": {"max_run_cost_usd": 1.0,
                       "estimate": {"gen_usd": 0.2, "judge_usd": 0.05},
                       "failure_breaker": {"consecutive_errors": 4, "error_rate": 0.5,
                                           "min_cases": 5}},
        "graded": {"specs_dir": str(root / "datasets/graded-specs"), "repeat": 3,
                   "pass_threshold": 3, "max_cells_per_suite": 8, "holdout": 0.25,
                   "calibration": {"samples": 3, "pass_min": 4, "fail_max": 2}},
        "observability": {"runs_dir": str(root / "outputs/runs"),
                          "history_dir": str(root / "outputs/history")},
    }
    config_path = root / "eval_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    monkeypatch.setenv("EVAL_CONFIG", str(config_path))
    config.load(force_reload=True)

    yield SimpleNamespace(root=root, config=cfg, suite_id="demo-suite",
                          asserts_ref=asserts_ref)

    monkeypatch.delenv("EVAL_CONFIG")
    config.load(force_reload=True)

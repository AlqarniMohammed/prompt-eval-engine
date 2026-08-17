import json

import pytest

from src.loaders import bundle_transform, pf_tests
from src.utils.bundle import parse_bundle


def test_offline_generator_passes_cases_through(synthetic_project, monkeypatch):
    monkeypatch.setenv("EVAL_OFFLINE", "1")
    tests = pf_tests.generate_tests()
    assert len(tests) >= 1
    assert all("provider" not in t for t in tests)
    assert all(t["vars"].get("suite") for t in tests)


def test_live_generator_requires_run_dir(synthetic_project, monkeypatch):
    monkeypatch.setenv("EVAL_SUITE", "demo-suite")
    with pytest.raises(RuntimeError, match="EVAL_RUN_DIR"):
        pf_tests.generate_tests()


def test_live_generator_materializes_isolated_workspaces(synthetic_project, monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("EVAL_SUITE", "demo-suite")
    monkeypatch.setenv("EVAL_REPEAT", "2")
    tests = pf_tests.generate_tests()
    workdirs = [t["vars"]["workdir"] for t in tests]
    assert len(set(workdirs)) == len(workdirs), "every trial owns its own workspace"
    for t in tests:
        # The agent-sdk override must ride in `options` — promptfoo silently
        # ignores test-level `provider:` dicts from Python generators.
        assert "provider" not in t
        cfg = t["options"]
        assert cfg["working_dir"] == t["vars"]["workdir"]
        assert cfg["env"]["EVAL_TRIAL"] == t["vars"]["trial"]
        assert cfg["model"], "generation model must be pinned per test"
        assert t["options"]["transform"] == pf_tests.TRANSFORM
        manifest = json.loads((tmp_path / "work").joinpath(
            cfg["working_dir"].split("/work/")[-1], ".workspace-manifest.json").read_text())
        assert manifest, "fixture files inventoried for the bundle transform"


def test_workspace_cache_identity_deterministic(synthetic_project, monkeypatch, tmp_path):
    """promptfoo fingerprints workspaces by MTIME and hashes the child env
    into the cache key — both must be identical across runs or generation
    cache replay never fires."""
    mtimes = {}
    for run in ("run-a", "run-b"):
        monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path / run))
        monkeypatch.setenv("EVAL_SUITE", "demo-suite")
        monkeypatch.setenv("EVAL_REPEAT", "1")
        tests = pf_tests.generate_tests()
        from pathlib import Path
        ws = Path(tests[0]["vars"]["workdir"])
        mtimes[run] = {str(p.relative_to(ws)): p.stat().st_mtime
                       for p in sorted(ws.rglob("*"))}
        assert tests[0]["options"]["env"]["EVAL_RUN_DIR"] == "", \
            "per-run dir must be pinned out of the provider's hashed env"
    assert mtimes["run-a"] == mtimes["run-b"], \
        "workspace mtimes must not vary across materializations"


def test_compare_generator_expands_both_variants(synthetic_project, monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("EVAL_SUITE", "demo-suite")
    monkeypatch.setenv("EVAL_REPEAT", "1")
    tests = pf_tests.generate_compare_tests()
    variants = {t["vars"]["promptVariant"] for t in tests}
    assert variants == {"current", "candidate"}


def test_transform_bundles_created_files(tmp_path):
    (tmp_path / ".workspace-manifest.json").write_text(json.dumps({"existing.md": "0" * 64}))
    (tmp_path / "existing.md").write_text("was here before")
    (tmp_path / "new.md").write_text("agent wrote this")
    out = bundle_transform.get_transform("final message", {"vars": {"workdir": str(tmp_path)}})
    parsed = parse_bundle(out)
    assert parsed["stdout"] == "final message"
    assert "new.md" in parsed["files"]
    assert "existing.md" in parsed["files"], "modified files (sha changed) are included"
    assert ".workspace-manifest.json" not in parsed["files"]


def test_transform_flags_hollow_write_case(tmp_path):
    (tmp_path / ".workspace-manifest.json").write_text("{}")
    with pytest.raises(RuntimeError, match="hollow output"):
        bundle_transform.get_transform(
            "ok", {"vars": {"workdir": str(tmp_path), "allowedTools": "Read,Write,Edit"}})


def test_transform_passes_offline_output_through():
    assert bundle_transform.get_transform("raw golden", {"vars": {}}) == "raw golden"


def test_plan_counts_matches_loader_under_eval_adaptive_k(synthetic_project, monkeypatch):
    """The runner's estimate and the loader must agree on per-case k — a
    mismatch means the budget gate prices a different run than executes."""
    from src import config, runner
    monkeypatch.setitem(config.get()["graded"], "adaptive_k",
                        {"enabled": True, "screen_suites": ["demo-suite"]})
    cases = [{"vars": {"suite": "demo-suite"}}, {"vars": {"suite": "demo-suite-2"}}]

    _, _, ks = runner._plan_counts("graded", cases, 3)
    loader_ks = list(pf_tests._graded_repeat(cases).values())
    assert ks == loader_ks == [1, 3]  # screening on

    monkeypatch.setenv("EVAL_ADAPTIVE_K", "0")
    _, _, ks = runner._plan_counts("graded", cases, 3)
    loader_ks = list(pf_tests._graded_repeat(cases).values())
    assert ks == loader_ks == [3, 3]  # screening disabled

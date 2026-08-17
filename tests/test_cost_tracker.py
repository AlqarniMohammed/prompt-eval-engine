import json

from src.utils import cost_tracker


def test_cost_usd_all_four_rates():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
    }
    r = cost_tracker.cost_usd("claude-sonnet-5", usage)
    assert r == 3 + 15 + 0.3 + 3.75


def test_unknown_model_overcounts_never_undercounts():
    usage = {"output_tokens": 1_000_000}
    assert cost_tracker.cost_usd("mystery-model", usage) >= cost_tracker.cost_usd("claude-opus-5", usage)


def test_ledger_roundtrip_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    cost_tracker.record("generation", "claude-sonnet-5", usage={"input_tokens": 100, "output_tokens": 200})
    cost_tracker.record("judge", "claude-haiku-4-5", usage={"input_tokens": 50, "output_tokens": 10})
    s = cost_tracker.summary_for_run(tmp_path)
    assert s["calls"] == 2
    assert set(s["byKind"]) == {"generation", "judge"}
    assert s["totalUsd"] > 0


def test_partial_ledger_line_is_skipped(tmp_path):
    (tmp_path / "spend.jsonl").write_text(
        json.dumps({"kind": "judge", "model": "m", "usage": {}, "usd": 0.5}) + "\n" + '{"kind": "gen'
    )
    assert cost_tracker.summary_for_run(tmp_path)["calls"] == 1


def test_exact_cap_hits_flags_truncation_signature():
    entries = [
        {"usage": {"output_tokens": 64000}},
        {"usage": {"output_tokens": 63999}},
    ]
    assert len(cost_tracker.exact_cap_hits(entries, 64000)) == 1


def test_measured_estimates_reads_seeded_history(tmp_path, monkeypatch):
    record = {
        "manifest": {"mode": "graded", "repeat": 2, "cases": 5,
                     "spend": {"calls": 15,
                               "byKind": {"generation": {"calls": 10, "usd": 2.0},
                                          "judge": {"calls": 5, "usd": 0.5}}}},
        # one cases[] row per executed trial: 5 cells x repeat 2
        "cases": [{"suite": "demo-suite"}] * 10,
    }
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "graded-20260101-000000.json").write_text(json.dumps(record))
    monkeypatch.setattr(cost_tracker.config, "history_dir", lambda: hist)
    est = cost_tracker.measured_estimates(None)
    assert est is not None
    assert abs(est["gen_usd_per_trial"] - 2.0 / 10) < 1e-9
    assert abs(est["judge_usd_per_call"] - 0.1) < 1e-9
    assert cost_tracker.measured_estimates("demo-suite") is not None
    assert cost_tracker.measured_estimates("absent-suite") is None
    # empty history means no measured basis, never a guess
    monkeypatch.setattr(cost_tracker.config, "history_dir", lambda: tmp_path / "none")
    assert cost_tracker.measured_estimates(None) is None


def test_measured_estimates_matches_suite_anywhere_in_record(tmp_path, monkeypatch):
    # Regression (audit finding 10): a record used to match a suite only via
    # its FIRST case row — suite "b" in a multi-suite run never matched.
    record = {
        "manifest": {"mode": "graded", "repeat": 1, "cases": 10,
                     "spend": {"calls": 10,
                               "byKind": {"generation": {"calls": 10, "usd": 1.0},
                                          "judge": {"calls": 10, "usd": 0.2}}}},
        "cases": [{"suite": "a"}] * 6 + [{"suite": "b"}] * 4,
    }
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "graded-20260101-000000.json").write_text(json.dumps(record))
    monkeypatch.setattr(cost_tracker.config, "history_dir", lambda: hist)
    est = cost_tracker.measured_estimates("b")
    assert est is not None
    assert abs(est["gen_usd_per_trial"] - 0.1) < 1e-9  # record-wide avg over 10 rows
    assert cost_tracker.measured_estimates("c") is None


def test_measured_estimates_counts_actual_rows_not_formula(tmp_path, monkeypatch):
    # Adaptive-k mix: manifest says 5 cases x repeat 3, but only 7 trials ran.
    record = {
        "manifest": {"mode": "graded", "repeat": 3, "cases": 5,
                     "spend": {"calls": 7,
                               "byKind": {"generation": {"calls": 7, "usd": 1.4}}}},
        "cases": [{"suite": "demo-suite"}] * 7,
    }
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "graded-20260101-000000.json").write_text(json.dumps(record))
    monkeypatch.setattr(cost_tracker.config, "history_dir", lambda: hist)
    est = cost_tracker.measured_estimates("demo-suite")
    assert abs(est["gen_usd_per_trial"] - 0.2) < 1e-9  # 1.4 / 7 rows, not / 15


def test_estimate_is_cache_aware():
    full = cost_tracker.estimate_run_usd(10, 10, None, expected_cache_hits=0)
    cached = cost_tracker.estimate_run_usd(10, 10, None, expected_cache_hits=10)
    assert cached["usd"] < full["usd"]


def test_calibration_estimate_counts_only_target_suite_goldens(synthetic_project):
    # Regression: the gate once estimated a 4-golden suite at the fleet-wide
    # average and aborted a ~$0.20 run at $3.25. The synthetic suite has one
    # golden dir with pass.txt + fail.txt (no mid) = 2 files.
    from src import config
    from src.runner import calibration_judge_calls

    by_id = {s["id"]: s for s in config.suites()}
    k = config.get()["graded"]["calibration"]["samples"]
    assert calibration_judge_calls([by_id["demo-suite"]]) == 2 * k
    # all suites together = the sum of each suite's own files
    total = sum(calibration_judge_calls([s]) for s in config.suites())
    assert calibration_judge_calls(list(config.suites())) == total


def test_calibration_judge_rate_reads_calibrate_ledgers_only(tmp_path, monkeypatch):
    # Calibration must never inherit the graded-run judge rate (transcripts
    # cost ~10x golden bundles): the rate comes from calibrate-* ledgers.
    import json
    from src.utils import cost_tracker

    run = tmp_path / "calibrate-20260101-000000"
    run.mkdir()
    entries = [{"kind": "judge", "usd": 0.016, "usage": {"input_tokens": 2000, "output_tokens": 600}}] * 4
    (run / "spend.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    graded = tmp_path / "graded-20260102-000000"
    graded.mkdir()
    (graded / "spend.jsonl").write_text(json.dumps({"kind": "judge", "usd": 0.17, "usage": {}}))

    monkeypatch.setattr(cost_tracker.config, "outputs_dir", lambda: tmp_path)
    rate = cost_tracker.measured_calibration_judge_rate()
    assert rate["source"] == "calibrate-20260101-000000"
    assert abs(rate["judge_usd_per_call"] - 0.016) < 1e-9

    monkeypatch.setattr(cost_tracker.config, "outputs_dir", lambda: tmp_path / "empty")
    assert cost_tracker.measured_calibration_judge_rate() is None


# ------------------------------------------------- E5: ceilings + rollup —
def test_role_caps_and_breach(synthetic_project, monkeypatch):
    from src import config as _config
    cfg = dict(_config.get())
    cfg["governance"] = {**cfg["governance"], "max_cost_usd": {"judge": 0.10, "generation": 1.0}}
    monkeypatch.setattr(_config, "_cached", cfg)
    caps = cost_tracker.role_caps()
    assert caps == {"judge": 0.10, "generation": 1.0}
    summary = {"byKind": {"judge": {"usd": 0.25}, "generation": {"usd": 0.5}}}
    assert "judge spend $0.25 > role cap $0.10" == cost_tracker.breached_role_cap(summary, caps)
    assert cost_tracker.breached_role_cap({"byKind": {}}, caps) is None
    assert cost_tracker.breached_role_cap(summary, {}) is None


def test_append_rollup_is_idempotent_and_skips_zero_spend(synthetic_project, tmp_path):
    rdir = tmp_path / "graded-x"
    rdir.mkdir()
    # zero spend → nothing recorded
    assert cost_tracker.append_rollup(rdir, "graded-x", "graded", "demo-suite") is None
    (rdir / "spend.jsonl").write_text(
        '{"kind": "generation", "usd": 0.5, "at": "2026-08-15T00:00:00+00:00"}\n'
        '{"kind": "alert", "usd": 0.0, "at": "2026-08-15T00:00:01+00:00"}\n')
    entry = cost_tracker.append_rollup(rdir, "graded-x", "graded", "demo-suite")
    assert entry["totalUsd"] == 0.5 and entry["byKind"] == {"generation": 0.5}
    assert cost_tracker.append_rollup(rdir, "graded-x", "graded", "demo-suite") is None
    lines = cost_tracker.rollup_path().read_text().splitlines()
    assert len(lines) == 1


def test_window_spend_rolls_off_old_entries(synthetic_project):
    import json as _json
    from datetime import datetime, timedelta, timezone as _tz
    now = datetime.now(_tz.utc)
    path = cost_tracker.rollup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"runId": "old", "totalUsd": 5.0, "at": (now - timedelta(hours=30)).isoformat()},
        {"runId": "new", "totalUsd": 1.5, "at": (now - timedelta(hours=2)).isoformat()},
    ]
    path.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    assert cost_tracker.window_spend(24) == 1.5
    assert cost_tracker.window_spend(168) == 6.5


def test_window_spend_counts_unrolled_live_runs(synthetic_project):
    import json as _json
    from datetime import datetime, timezone as _tz
    from src import config as _config
    rdir = _config.outputs_dir() / "graded-live"
    rdir.mkdir(parents=True)
    (rdir / "spend.jsonl").write_text(_json.dumps(
        {"kind": "judge", "usd": 0.2, "at": datetime.now(_tz.utc).isoformat()}) + "\n")
    assert abs(cost_tracker.window_spend(24) - 0.2) < 1e-9

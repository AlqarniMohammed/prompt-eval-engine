"""E6: blocked-spend events, cache-hit metering, savings lines, live meter."""

import json

from src import config, hooks, runner, state
from src.science.gen_matrix import generate
from src.utils import cost_tracker, proc, status

SUITE = "demo-suite"


def test_cached_row_meters_the_avoided_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    hooks._after_each({
        "test": {"vars": {"cell": "c1"}},
        "result": {"success": True, "failureReason": 0, "gradingResult": {"pass": True},
                   "response": {"cached": True, "cost": 0.42}},
    })
    entries = cost_tracker.read_ledger(tmp_path)
    hits = [e for e in entries if e["kind"] == "cache_hit"]
    assert len(hits) == 1
    assert hits[0]["usd"] == 0.0 and hits[0]["saved_usd"] == 0.42
    # real totals stay real spend only
    assert cost_tracker.summary_for_run(tmp_path)["totalUsd"] == 0.0


def test_measured_estimates_ignore_cache_hit_kind(synthetic_project):
    hist = config.history_dir()
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "graded-20260101-000000.json").write_text(json.dumps({
        "manifest": {"suite": SUITE, "mode": "graded",
                     "spend": {"calls": 3,
                               "byKind": {"generation": {"usd": 1.0, "calls": 2},
                                          "judge": {"usd": 0.2, "calls": 2},
                                          "cache_hit": {"usd": 0.0, "calls": 5}}}},
        "cases": [{"suite": SUITE}, {"suite": SUITE}],
    }))
    m = cost_tracker.measured_estimates(SUITE)
    assert m["gen_usd_per_trial"] == 0.5
    assert m["judge_usd_per_call"] == 0.1


def test_budget_abort_records_a_blocked_event(synthetic_project, capsys):
    generate(config.specs_dir() / f"{SUITE}.yaml")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    state.record(SUITE, "calibrated", {**state.calibration_fingerprint(SUITE), "green": True})
    state.record_preflight(state.preflight_fingerprint())
    state.record_smoked(SUITE, "graded-smoke")
    rc = runner.main(["graded", "--suite", SUITE])  # $1 ceiling < ~$4.50 estimate
    assert rc == 1
    blocked = [json.loads(l) for l in cost_tracker.blocked_path().read_text().splitlines()]
    assert len(blocked) == 1
    assert blocked[0]["command"] == "graded" and blocked[0]["gate"] == "run-ceiling"
    assert blocked[0]["estimateUsd"] > blocked[0]["ceilingUsd"]


def test_savings_totals_and_round_line(synthetic_project, capsys):
    rdir = config.outputs_dir() / "graded-a"
    rdir.mkdir(parents=True)
    (rdir / "spend.jsonl").write_text(json.dumps(
        {"kind": "cache_hit", "usd": 0.0, "saved_usd": 0.3, "at": "2026-08-15T00:00:00+00:00"}) + "\n")
    cost_tracker.record_blocked("graded", SUITE, 2.5, 1.0, "run-ceiling", "test")
    totals = cost_tracker.savings_totals()
    assert totals == {"cacheHits": 1, "cacheSavedUsd": 0.3,
                      "blockedCount": 1, "blockedUsd": 2.5}
    runner.main(["round"])
    out = capsys.readouterr().out
    assert "saved" in out and "$0.30" in out and "$2.50" in out


def test_progress_note_shape():
    events = [{"ev": "case_end", "cached": True}, {"ev": "case_end", "cached": False},
              {"ev": "progress"}]
    note = proc.progress_note({"totalUsd": 0.5}, 2.0, events, planned=10)
    assert note == "$0.50 of $2.00 (25%) · 2/10 cells · 1 cached"


def test_watchdog_emits_progress_line(tmp_path):
    # emit-on-change: same summary twice → one progress event
    seen = []
    summary = {"totalUsd": 0.1}
    last = [None]
    for _ in range(2):
        note = proc.progress_note(summary, 1.0, [], None)
        if note != last[0]:
            last[0] = note
            seen.append(note)
    assert len(seen) == 1

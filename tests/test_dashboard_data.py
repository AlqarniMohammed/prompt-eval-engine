"""Dashboard data layer — pure functions against tmp artifacts plus committed
fixtures holding trimmed REAL campaign records (recovered from git history at
the pre-bare commit). Tolerance is the contract under test: torn files,
missing files, and dead pids must degrade, never raise."""

import json
import os
import shutil
from pathlib import Path

import pytest

from src.dashboard import data
from src.dashboard.pipeline_docs import STAGE_DOCS, ordered_stages

FIXTURES = Path(__file__).parent / "fixtures" / "dashboard"
NOW = 1_800_000_000.0  # injectable clock — tests never depend on wall time


def _mkrun(runs_dir: Path, run_id: str, manifest: dict | None = None,
           events: list | None = None, ledger: list | None = None) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True)
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest))
    if events:
        (d / "status.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if ledger:
        (d / "spend.jsonl").write_text("\n".join(json.dumps(e) for e in ledger) + "\n")
    return d


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ---------------------------------------------------------------- readers —
def test_safe_json_and_jsonl_tolerate_torn_files(tmp_path):
    torn = tmp_path / "torn.json"
    torn.write_text('{"a": 1, "b"')  # mid-rewrite
    assert data.safe_json(torn) is None
    assert data.safe_json(tmp_path / "missing.json") is None

    lines = tmp_path / "x.jsonl"
    lines.write_text('{"ok": 1}\n{"partial": ')  # writer mid-append
    assert data.read_jsonl(lines) == [{"ok": 1}]
    assert data.read_jsonl(tmp_path / "missing.jsonl") == []


# ------------------------------------------------------------------- runs —
def test_list_runs_orders_and_classifies(tmp_path):
    _mkrun(tmp_path, "graded-20260101-000000", {
        "runId": "graded-20260101-000000", "mode": "graded", "suite": "s",
        "startedAt": "2026-01-01T00:00:00+00:00", "finishedAt": "2026-01-01T00:10:00+00:00",
        "cases": 6, "repeat": 3, "spend": {"totalUsd": 1.5, "calls": 30},
        "killedReason": None})
    _mkrun(tmp_path, "graded-20260102-000000", {
        "runId": "graded-20260102-000000", "mode": "graded", "suite": "s",
        "cases": 6, "repeat": 3, "killedReason": "spend kill-switch: $2 > $1"})
    (tmp_path / "validate-20260103-000000").mkdir()
    torn = _mkrun(tmp_path, "graded-20260104-000000")
    (torn / "manifest.json").write_text('{"runId": "gr')  # torn manifest → skipped, no raise

    rows = data.list_runs(tmp_path)
    assert [r["runId"] for r in rows] == [
        "validate-20260103-000000", "graded-20260102-000000", "graded-20260101-000000"]
    assert rows[0]["status"] == "offline"
    assert rows[1]["status"] == "failed"
    assert rows[2]["status"] == "finished" and rows[2]["spendUsd"] == 1.5


def test_run_detail_reads_ledger_fresh_and_missing_returns_none(tmp_path):
    _mkrun(tmp_path, "graded-x", {"runId": "graded-x",
                                  "effectiveAgents": {"generation": {"max_tokens_per_turn": 100}}},
           events=[{"ts": _iso(NOW), "ev": "case_end", "ok": True}],
           ledger=[{"kind": "generation", "usd": 0.5, "usage": {"output_tokens": 100}},
                   {"kind": "alert", "usd": 0, "note": "cap"}])
    d = data.run_detail(tmp_path, "graded-x")
    assert d["spend"]["totalUsd"] == 0.5
    assert d["capHits"] == 1  # output landed exactly on the 100-token cap
    assert len(d["alerts"]) == 1
    assert data.run_detail(tmp_path, "nope") is None


# ------------------------------------------------------------------- live —
def _live_manifest(cases=4, repeat=1, mode="graded", **extra):
    return {"runId": "graded-live", "mode": mode, "suite": "s", "cases": cases,
            "repeat": repeat, "startedAt": _iso(NOW - 100),
            "effectiveAgents": {"generation": {"max_tokens_per_turn": 64000}}, **extra}


def test_live_snapshot_alive_pid_lock_progress_eta_and_ceiling(tmp_path):
    runs = tmp_path / "runs"
    events = [{"ts": _iso(NOW - 100), "ev": "run_banner"}] + [
        {"ts": _iso(NOW - 80 + i * 10), "ev": "case_end", "ok": True} for i in range(2)]
    _mkrun(runs, "graded-live", _live_manifest(maxCostOverride=3.0), events=events,
           ledger=[{"kind": "judge", "usd": 0.4, "usage": {}}])
    (tmp_path / ".run.lock").write_text(json.dumps(
        {"pid": os.getpid(), "runId": "graded-live", "startedAt": _iso(NOW - 100)}))

    snap = data.live_snapshot(tmp_path, runs, heartbeat_seconds=30,
                              default_ceiling_usd=1.0, now=NOW)
    assert snap["active"] and snap["source"] == "lock"
    assert snap["done"] == 2 and snap["planned"] == 4
    assert snap["progress"] == 0.5
    assert snap["etaSeconds"] == pytest.approx(100.0)  # 100s elapsed / 2 done * 2 left
    assert snap["spendUsd"] == 0.4
    assert snap["ceilingUsd"] == 3.0 and snap["ceilingOverridden"]


def test_live_snapshot_dead_pid_falls_back_and_stall_flag(tmp_path):
    runs = tmp_path / "runs"
    d = _mkrun(runs, "validate-20260101-000000")
    (d / "offline-pass-pass.json").write_text("{}")
    (tmp_path / ".run.lock").write_text(json.dumps({"pid": 2 ** 22 + 12345, "runId": "gone"}))

    fresh = os.stat(d / "offline-pass-pass.json").st_mtime
    snap = data.live_snapshot(tmp_path, runs, 30, 1.0, now=fresh + 5)
    assert snap["active"] and snap["source"] == "mtime"
    assert snap["runId"] == "validate-20260101-000000"

    # past the freshness window: nothing live
    snap2 = data.live_snapshot(tmp_path, runs, 30, 1.0, now=fresh + 500)
    assert snap2["active"] is False

    # a locked run whose last event is older than 2x heartbeat is stalled
    runs2 = tmp_path / "runs2"
    _mkrun(runs2, "graded-live", _live_manifest(),
           events=[{"ts": _iso(NOW - 300), "ev": "case_end", "ok": True}])
    (tmp_path / ".run.lock").write_text(json.dumps({"pid": os.getpid(), "runId": "graded-live"}))
    old = NOW - 300
    os.utime(runs2 / "graded-live" / "status.jsonl", (old, old))
    os.utime(runs2 / "graded-live" / "manifest.json", (old, old))
    snap3 = data.live_snapshot(tmp_path, runs2, heartbeat_seconds=30,
                               default_ceiling_usd=1.0, now=NOW)
    assert snap3["active"] and snap3["stalled"] is True


# ---------------------------------------------------- history + evidence —
def test_history_series_from_real_records(tmp_path):
    hist = tmp_path / "history"
    shutil.copytree(FIXTURES / "history", hist)
    out = data.history_series(hist)

    assert set(out["series"]) == {"05-evaluate", "02-client-persona"}
    p = out["series"]["05-evaluate"][0]
    assert p["n"] == 4 and p["dims"]  # real trimmed record: 4 cases, scored dims
    for mean in p["dims"].values():
        assert 1 <= mean <= 5
    assert out["spendByMode"]["graded"]["runs"] == 2
    assert out["spendByMode"]["graded"]["usd"] > 0
    cal = out["calibrations"][0]
    assert cal["suite"] == "05-evaluate" and cal["green"] is True
    assert cal["judgeModel"] == "claude-sonnet-4-6"

    # torn history file degrades to "skipped"
    (hist / "graded-torn.json").write_text('{"manifest": {')
    assert data.history_series(hist)  # no raise


def test_verdict_list_normalizes_js_kit_pairwise_keys(tmp_path):
    hist = tmp_path / "history"
    shutil.copytree(FIXTURES / "history", hist)
    verdicts = data.verdict_list(hist)
    assert len(verdicts) == 2
    assert verdicts[0]["at"] > verdicts[1]["at"]  # newest first
    v = verdicts[1]
    assert v["promote"] is True and v["target"] == "stage_discipline"
    assert v["pairwise"] == {"winsCurrent": 0, "winsCandidate": 4, "ties": 2}


def test_evidence_summary_from_real_sidecar_lines(tmp_path):
    src = tmp_path / "judge-evidence.jsonl"
    shutil.copy(FIXTURES / "judge-evidence.jsonl", src)
    out = data.evidence_summary([src, tmp_path / "missing.jsonl"])
    assert out["scored"] == 2 and out["errorCount"] == 1
    assert out["worst"][0]["mean"] == 1.0  # the all-1s real record sorts first
    assert out["worst"][0]["cell"] == "c-bad"
    for buckets in out["histograms"].values():
        assert sum(buckets.values()) == 2
    assert out["suggestedFixes"] and out["suggestedFixes"][0]["count"] >= 1
    assert out["errors"][0]["cell"] == "c-mal"


def test_spend_accounting_sums_all_ledgers(tmp_path):
    _mkrun(tmp_path, "a", None, ledger=[{"kind": "generation", "usd": 1.0},
                                        {"kind": "alert", "usd": 0}])
    _mkrun(tmp_path, "b", None, ledger=[{"kind": "judge", "usd": 0.25}])
    out = data.spend_accounting(tmp_path)
    assert out["total"] == {"usd": 1.25, "calls": 2}
    assert out["alerts"] == 1
    assert out["byKind"]["judge"]["calls"] == 1


# -------------------------------------------------------- state + docs —
def test_pipeline_view_stages_staleness_and_placeholders(synthetic_project):
    from src import state
    sid = synthetic_project.suite_id
    state.record(sid, "validated", state.validate_fingerprint(sid))
    state.record(sid, "calibrated", {**state.calibration_fingerprint(sid),
                                     "green": True, "record": "x.json"})
    view = data.pipeline_view()
    suite = next(s for s in view["suites"] if s["suite"] == sid)
    by_stage = {s["stage"]: s for s in suite["stages"]}
    assert by_stage["validated"]["done"] and not by_stage["validated"]["stale"]
    assert by_stage["calibrated"]["green"] is True
    assert not by_stage["baselined"]["done"]
    assert suite["next"] == "baselined"
    assert view["preflight"]["recorded"] is False

    # rubric edit → calibrated goes stale on rubric_sha
    rubric = synthetic_project.root / "judges/demo.md"
    rubric.write_text(rubric.read_text() + "\n<!-- edited -->\n")
    view2 = data.pipeline_view()
    suite2 = next(s for s in view2["suites"] if s["suite"] == sid)
    cal = next(s for s in suite2["stages"] if s["stage"] == "calibrated")
    assert cal["stale"] == ["rubric_sha"]
    assert suite2["next"] == "calibrated"


def test_stage_docs_cover_every_stage_and_real_commands():
    from src import state
    from src.runner import build_parser
    for stage in state.STAGES:
        assert stage in STAGE_DOCS, f"STAGE_DOCS missing {stage}"
    for extra in ("smoked", "preflight", "verdict"):
        assert extra in STAGE_DOCS
    subcommands = None
    for action in build_parser()._subparsers._group_actions:
        subcommands = set(action.choices)
    for key, doc in STAGE_DOCS.items():
        assert doc["command"] in subcommands, f"{key} names unknown command {doc['command']}"
        for field in ("title", "what", "proves", "benefit", "cost"):
            assert doc.get(field), f"{key} missing {field}"
    assert ordered_stages()[:6] == state.STAGES
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    for stage in state.STAGES:
        assert stage in readme, f"README never names pipeline stage {stage!r}"

"""Pure aggregation for the dashboard: Paths in, JSON-able dicts out.

Tolerance is the design rule — every reader here must survive partial,
missing, or mid-rewrite files (the engine appends JSONL while we read, and
manifests are rewritten at run end), returning best-effort data instead of
raising. Nothing in this module writes, caches, or touches process env.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from src.utils import cost_tracker


# ------------------------------------------------------- tolerant readers —
def safe_json(path: Path):
    """None on missing/torn/mid-rewrite files — the next poll self-heals."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def read_jsonl(path: Path) -> list[dict]:
    """Partial-line tolerant (the writer may be mid-append)."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False
    except PermissionError:
        return True


def _newest_mtime(directory: Path) -> float | None:
    try:
        times = [p.stat().st_mtime for p in directory.rglob("*") if p.is_file()]
        return max(times) if times else directory.stat().st_mtime
    except OSError:
        return None


# ------------------------------------------------------------------- runs —
def list_runs(runs_dir: Path, limit: int = 200) -> list[dict]:
    """Every run directory, newest first — manifest-bearing runs summarized,
    lockless validate dirs included as offline entries."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    rows = []
    for d in runs_dir.iterdir():
        if not d.is_dir():
            continue
        manifest = safe_json(d / "manifest.json")
        if manifest:
            spend = manifest.get("spend") or {}
            rows.append({
                "runId": manifest.get("runId") or d.name,
                "mode": manifest.get("mode"),
                "suite": manifest.get("suite"),
                "startedAt": manifest.get("startedAt"),
                "finishedAt": manifest.get("finishedAt"),
                "cases": manifest.get("cases"),
                "repeat": manifest.get("repeat"),
                "models": manifest.get("models"),
                "forcedGates": len(manifest.get("forcedGates") or []),
                "spendUsd": spend.get("totalUsd"),
                "calls": spend.get("calls"),
                "killedReason": manifest.get("killedReason"),
                "status": ("failed" if manifest.get("killedReason")
                           else "finished" if manifest.get("finishedAt")
                           else "in-flight"),
            })
        elif d.name.startswith("validate-"):
            rows.append({"runId": d.name, "mode": "validate", "suite": None,
                         "startedAt": None, "finishedAt": None, "cases": None,
                         "repeat": None, "models": None, "forcedGates": 0,
                         "spendUsd": 0.0, "calls": 0, "killedReason": None,
                         "status": "offline"})
    rows.sort(key=lambda r: r["runId"], reverse=True)
    return rows[:limit]


def run_detail(runs_dir: Path, run_id: str) -> dict | None:
    run_dir = Path(runs_dir) / run_id
    if not run_dir.is_dir():
        return None
    manifest = safe_json(run_dir / "manifest.json") or {}
    manifest.pop("verbatimConfig", None)  # bulky; the file itself stays on disk
    entries = cost_tracker.read_ledger(run_dir)
    spend = cost_tracker.summarize([e for e in entries if e.get("kind") in ("generation", "judge")])
    cap = int(((manifest.get("effectiveAgents") or {}).get("generation") or {})
              .get("max_tokens_per_turn") or 64000)
    events = read_jsonl(run_dir / "status.jsonl")
    report = None
    try:
        report_path = run_dir / "report.md"
        if report_path.exists():
            report = report_path.read_text()[:20000]
    except (OSError, UnicodeDecodeError):
        pass
    return {
        "runId": run_id,
        "manifest": manifest,
        "spend": spend,
        "alerts": [e for e in entries if e.get("kind") == "alert"],
        "capHits": len(cost_tracker.exact_cap_hits(entries, cap)),
        "events": events[-50:],
        "eventCount": len(events),
        "report": report,
        "evidenceCount": len(read_jsonl(run_dir / "judge-evidence.jsonl")),
    }


# ------------------------------------------------------------------- live —
LOCKLESS_FRESH_SECONDS = 90


def live_snapshot(outputs_root: Path, runs_dir: Path, heartbeat_seconds: float,
                  default_ceiling_usd: float, now: float | None = None) -> dict:
    """The live panel: active-run detection (lock pid-liveness, with an mtime
    fallback for lockless validate runs), progress/ETA from case_end events,
    measured spend vs the effective ceiling, and a stalled flag."""
    now = time.time() if now is None else now
    outputs_root, runs_dir = Path(outputs_root), Path(runs_dir)
    lock = safe_json(outputs_root / ".run.lock")
    active_run_id = None
    source = None
    if lock and _pid_alive(lock.get("pid")):
        active_run_id = lock.get("runId")
        source = "lock"
    else:
        # Lockless fallback: validate runs take no lock — a run dir whose
        # newest file changed within the window counts as live.
        candidates = sorted((d for d in runs_dir.iterdir() if d.is_dir()),
                            key=lambda d: d.name, reverse=True) if runs_dir.is_dir() else []
        for d in candidates[:5]:
            mtime = _newest_mtime(d)
            if mtime and now - mtime < LOCKLESS_FRESH_SECONDS:
                active_run_id = d.name
                source = "mtime"
                break

    if not active_run_id:
        return {"active": False, "now": now}

    run_dir = runs_dir / active_run_id
    manifest = safe_json(run_dir / "manifest.json") or {}
    events = read_jsonl(run_dir / "status.jsonl")
    case_ends = [e for e in events if e.get("ev") == "case_end"]
    failures = sum(1 for e in case_ends if e.get("ok") is False)
    mode = manifest.get("mode") or ("validate" if active_run_id.startswith("validate-") else None)
    variants = 2 if mode in ("compare", "confirm") else 1
    planned = None
    if manifest.get("cases") and manifest.get("repeat"):
        planned = manifest["cases"] * manifest["repeat"] * variants

    started = _ts(manifest.get("startedAt")) or (_ts(events[0]["ts"]) if events else None)
    eta_seconds = None
    if planned and case_ends and started:
        elapsed = max(0.0, now - started)
        rate = elapsed / len(case_ends)
        eta_seconds = max(0.0, rate * (planned - len(case_ends)))

    entries = cost_tracker.read_ledger(run_dir)
    spend = cost_tracker.summarize([e for e in entries if e.get("kind") in ("generation", "judge")])
    ceiling = manifest.get("maxCostOverride") or default_ceiling_usd
    cap = int(((manifest.get("effectiveAgents") or {}).get("generation") or {})
              .get("max_tokens_per_turn") or 64000)

    event_times = [t for t in (_ts(e.get("ts")) for e in events) if t]
    last_activity = max([t for t in [max(event_times) if event_times else None,
                                     _newest_mtime(run_dir)] if t], default=None)
    stalled = bool(last_activity and (now - last_activity) > 2 * heartbeat_seconds)

    return {
        "active": True,
        "now": now,
        "source": source,
        "runId": active_run_id,
        "mode": mode,
        "suite": manifest.get("suite"),
        "models": manifest.get("models"),
        "startedAt": manifest.get("startedAt") or (lock or {}).get("startedAt"),
        "planned": planned,
        "done": len(case_ends),
        "failures": failures,
        "progress": (len(case_ends) / planned) if planned else None,
        "etaSeconds": eta_seconds,
        "spendUsd": spend.get("totalUsd", 0.0),
        "calls": spend.get("calls", 0),
        "byKind": spend.get("byKind", {}),
        "ceilingUsd": ceiling,
        "ceilingOverridden": "maxCostOverride" in manifest,
        "capHits": len(cost_tracker.exact_cap_hits(entries, cap)),
        "alerts": [e.get("note") for e in entries if e.get("kind") == "alert"][-5:],
        "stalled": stalled,
        "lastActivity": last_activity,
        "events": events[-30:],
    }


# ----------------------------------------------------------------- state —
def pipeline_view() -> dict:
    """Per-suite state machine with staleness vs CURRENT fingerprints, plus
    the preflight record and next actions. Reads config+state, writes nothing."""
    from src import config, state
    from src.reports import checks

    st = state.load()
    suites = []
    for suite in config.suites():
        sid = suite["id"]
        entry = (st.get("suites") or {}).get(sid, {})
        stages = []
        for stage in state.STAGES:
            record = entry.get(stage)
            stale: list[str] = []
            if record:
                want = {}
                try:
                    if stage == "validated":
                        want = state.validate_fingerprint(sid)
                    elif stage == "calibrated":
                        want = state.calibration_fingerprint(sid)
                    elif stage == "baselined":
                        want = {}
                except Exception:  # noqa: BLE001 — a half-edited config must not 500 the panel
                    want = {}
                stale = [k for k, v in want.items() if v is not None and record.get(k) != v]
            stages.append({
                "stage": stage,
                "done": bool(record),
                "at": (record or {}).get("at"),
                "stale": stale,
                "restored": bool((record or {}).get("restored_from")),
                "green": (record or {}).get("green"),
                "verdict": (record or {}).get("verdict"),
                "runId": (record or {}).get("runId"),
            })
        smoke_problems = []
        try:
            smoke_problems = state.check_smoked(sid)
        except Exception:  # noqa: BLE001
            pass
        nxt = next((s["stage"] for s in stages if not s["done"] or s["stale"]), None)
        suites.append({
            "suite": sid,
            "rubric": bool(suite.get("rubric")),
            "stages": stages,
            "smoked": not smoke_problems,
            "smokeProblems": smoke_problems,
            "placeholders": checks.placeholder_files(suite),
            "next": nxt,
        })
    preflight = st.get("preflight")
    try:
        preflight_problems = state._check_preflight()
    except Exception:  # noqa: BLE001
        preflight_problems = []
    return {"suites": suites,
            "preflight": {"recorded": bool(preflight),
                          "at": (preflight or {}).get("at"),
                          "problems": preflight_problems}}


# ---------------------------------------------------------------- history —
def _suite_rows(record: dict) -> dict[str, list[dict]]:
    by_suite: dict[str, list[dict]] = {}
    for case in record.get("cases") or []:
        if isinstance(case, dict) and case.get("suite"):
            by_suite.setdefault(case["suite"], []).append(case)
    return by_suite


def history_series(history_dir: Path) -> dict:
    """Dimension-mean series per suite across archived graded/compare runs,
    spend by mode and generation model, and the calibration timeline."""
    history_dir = Path(history_dir)
    series: dict[str, list[dict]] = {}
    spend_by_mode: dict[str, dict] = {}
    spend_by_model: dict[str, dict] = {}
    if history_dir.is_dir():
        for f in sorted(history_dir.glob("*.json")):
            if not (f.name.startswith("graded-") or f.name.startswith("compare-graded-")):
                continue
            record = safe_json(f)
            if not record:
                continue
            manifest = record.get("manifest") or {}
            spend = manifest.get("spend") or {}
            mode = manifest.get("mode") or "?"
            gen_model = (manifest.get("models") or {}).get("generation") or "?"
            if spend.get("totalUsd"):
                for table, key in ((spend_by_mode, mode), (spend_by_model, gen_model)):
                    slot = table.setdefault(key, {"usd": 0.0, "runs": 0})
                    slot["usd"] += spend["totalUsd"]
                    slot["runs"] += 1
            for sid, rows in _suite_rows(record).items():
                dims: dict[str, list] = {}
                succeeded = 0
                for case in rows:
                    if case.get("success"):
                        succeeded += 1
                    for k, v in (case.get("scores") or {}).items():
                        if isinstance(v, (int, float)):
                            dims.setdefault(k, []).append(v)
                point = {
                    "file": f.name,
                    "runId": manifest.get("runId"),
                    "mode": mode,
                    "at": manifest.get("startedAt"),
                    "n": len(rows),
                    "scored": sum(len(v) for v in dims.values()),
                    "successRate": (succeeded / len(rows)) if rows else None,
                    "dims": {k: sum(v) / len(v) for k, v in dims.items() if v},
                }
                series.setdefault(sid, []).append(point)
    for points in series.values():
        points.sort(key=lambda p: (p["at"] or "", p["file"]))

    calibrations = []
    if history_dir.is_dir():
        for f in sorted(history_dir.glob("calibration-*.json")):
            record = safe_json(f)
            if not record:
                continue
            stamp = f.stem.rsplit("-", 2)
            calibrations.append({
                "file": f.name,
                "suite": record.get("suite"),
                "green": record.get("green"),
                "judgeModel": record.get("judgeModel"),
                "samples": record.get("samples"),
                "goldens": len(record.get("goldens") or {}),
                "at": "-".join(stamp[-2:]) if len(stamp) == 3 else None,
            })
    return {"series": series, "spendByMode": spend_by_mode,
            "spendByGenModel": spend_by_model, "calibrations": calibrations}


def spend_accounting(runs_dir: Path) -> dict:
    """Measured spend across ALL run ledgers on disk (the live source of
    truth, including runs never archived)."""
    runs_dir = Path(runs_dir)
    total = {"usd": 0.0, "calls": 0}
    by_kind: dict[str, dict] = {}
    alerts = 0
    if runs_dir.is_dir():
        for ledger in runs_dir.glob("*/spend.jsonl"):
            for e in read_jsonl(ledger):
                kind = e.get("kind")
                if kind == "alert":
                    alerts += 1
                    continue
                usd = e.get("usd") or 0.0
                total["usd"] += usd
                total["calls"] += 1
                slot = by_kind.setdefault(kind or "?", {"usd": 0.0, "calls": 0})
                slot["usd"] += usd
                slot["calls"] += 1
    return {"total": total, "byKind": by_kind, "alerts": alerts}


# --------------------------------------------------------------- verdicts —
def verdict_list(history_dir: Path) -> list[dict]:
    history_dir = Path(history_dir)
    out = []
    for v in read_jsonl(history_dir / "verdicts.jsonl"):
        row = {"at": v.get("at"), "suite": v.get("suite"),
               "promote": v.get("promote"), "reason": v.get("reason"),
               "newEvidence": v.get("newEvidence"), "record": v.get("record")}
        record = safe_json(history_dir / v["record"]) if v.get("record") else None
        if record:
            pairwise = record.get("pairwise") or {}
            # The seeded JS-kit corpus used winsA/winsB; the engine writes
            # winsCurrent/winsCandidate — normalize so both render.
            normalized = {
                "winsCurrent": pairwise.get("winsCurrent", pairwise.get("winsA")),
                "winsCandidate": pairwise.get("winsCandidate", pairwise.get("winsB")),
                "ties": pairwise.get("ties"),
            } if pairwise else None
            row.update({
                "target": record.get("target"),
                "targetOk": record.get("targetOk"),
                "pairwise": normalized,
                "regressions": record.get("regressions"),
                "spread": record.get("spread"),
                "blocked": bool(record.get("blocked")),
                "dimMeans": record.get("dimMeans"),
            })
        out.append(row)
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out


# --------------------------------------------------------------- evidence —
def evidence_summary(sources: list[Path], worst_n: int = 10) -> dict:
    """Aggregate judge-evidence.jsonl records: per-dimension score histograms,
    the worst-scored cells with the judge's own words, deduped suggested
    prompt fixes, and judge errors."""
    records: list[dict] = []
    for src in sources:
        records += read_jsonl(src)
    scored = [r for r in records if isinstance(r.get("scores"), dict)]
    errors = [r for r in records if r.get("judgeError")]

    histograms: dict[str, dict[int, int]] = {}
    for r in scored:
        for dim, score in r["scores"].items():
            if isinstance(score, (int, float)):
                bucket = histograms.setdefault(dim, {s: 0 for s in range(1, 6)})
                key = min(5, max(1, int(round(score))))
                bucket[key] += 1

    def mean_of(r):
        values = [v for v in r["scores"].values() if isinstance(v, (int, float))]
        return sum(values) / len(values) if values else None

    worst = []
    for r in scored:
        m = mean_of(r)
        if m is None:
            continue
        judge = r.get("judge") or {}
        worst.append({
            "suite": r.get("suite"),
            "cell": r.get("cell"),
            "mean": m,
            "scores": r["scores"],
            "topIssue": str(judge.get("top_issue") or "")[:240],
            "vars": r.get("vars"),
            "ts": r.get("ts"),
        })
    worst.sort(key=lambda w: w["mean"])
    worst = worst[:worst_n]

    fixes: dict[str, dict] = {}
    for r in scored:
        fix = str((r.get("judge") or {}).get("suggested_prompt_fix") or "").strip()
        if not fix:
            continue
        key = fix.lower()
        slot = fixes.setdefault(key, {"fix": fix[:300], "count": 0, "suites": set()})
        slot["count"] += 1
        if r.get("suite"):
            slot["suites"].add(r["suite"])
    top_fixes = sorted(fixes.values(), key=lambda s: -s["count"])[:10]
    for s in top_fixes:
        s["suites"] = sorted(s["suites"])

    tags: dict[str, int] = {}
    for r in scored:
        tag = (r.get("judge") or {}).get("top_issue_tag")
        if isinstance(tag, str) and tag:
            tags[tag] = tags.get(tag, 0) + 1

    return {
        "failureTags": dict(sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))),
        "records": len(records),
        "scored": len(scored),
        "errors": [{"suite": e.get("suite"), "cell": e.get("cell"),
                    "error": str(e.get("judgeError"))[:200], "ts": e.get("ts")}
                   for e in errors[-20:]],
        "errorCount": len(errors),
        "histograms": histograms,
        "worst": worst,
        "suggestedFixes": top_fixes,
    }


def evidence_sources(outputs_root: Path, runs_dir: Path) -> list[Path]:
    """Every judge-evidence sidecar: per-run files plus the legacy root one."""
    sources = []
    runs_dir = Path(runs_dir)
    if runs_dir.is_dir():
        sources += sorted(runs_dir.glob("*/judge-evidence.jsonl"))
    legacy = Path(outputs_root) / "judge-evidence.jsonl"
    if legacy.exists():
        sources.append(legacy)
    return sources

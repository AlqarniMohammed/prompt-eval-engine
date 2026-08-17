"""Run-level spend accounting.

Every spending path appends to the run's spend.jsonl ledger (named by
EVAL_RUN_DIR): the afterEach hook records generation cost reported by the
native claude-agent-sdk provider; the judge client records its own calls.
Cached replays record nothing. The watchdog and the post-run manifest read
the ledger back — measured numbers, never cap-derived estimates.

Manifest/history JSON keeps the JS-kit camelCase schema (byKind, totalUsd,
usd, inputTokens, outputTokens) so the 40 seeded history records and the new
engine's records stay one uniform corpus for measured_estimates().
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.utils import chain


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cost_usd(model: str, usage: dict) -> float:
    """Cost of one API response's usage block (raw Anthropic shape)."""
    r = config.pricing(model)
    per = lambda tokens, rate: (tokens or 0) / 1_000_000 * (rate or 0)
    return (
        per(usage.get("input_tokens"), r.get("input"))
        + per(usage.get("output_tokens"), r.get("output"))
        + per(usage.get("cache_read_input_tokens"), r.get("cache_read"))
        + per(usage.get("cache_creation_input_tokens"), r.get("cache_write"))
    )


def run_dir() -> Path | None:
    d = os.environ.get("EVAL_RUN_DIR")
    return Path(d) if d else None


def ledger_path(rdir: Path) -> Path:
    return rdir / "spend.jsonl"


def record(kind: str, model: str, usage: dict | None = None, usd: float | None = None, **extra):
    """Record one live spend event. kind: 'generation' | 'judge'.

    Either usage (priced here) or usd (already measured, e.g. the native
    provider's total_cost_usd) must be given. Never raises — spend tracking
    must never fail a live call — but a missing EVAL_RUN_DIR means the caller
    is an unregistered entrypoint, which incident #14 forbids: surface loudly
    on stderr.
    """
    entry = {
        "kind": kind,
        "model": model,
        "usage": usage or {},
        "usd": usd if usd is not None else cost_usd(model, usage or {}),
        "at": _now(),
        **extra,
    }
    rdir = run_dir()
    if rdir is None:
        import sys
        print(f"[cost_tracker] WARN unmetered spend (no EVAL_RUN_DIR): {kind} ${entry['usd']:.4f}", file=sys.stderr)
        return entry
    try:
        rdir.mkdir(parents=True, exist_ok=True)
        with ledger_path(rdir).open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        import sys
        print(f"[cost_tracker] WARN ledger append failed: {e}", file=sys.stderr)
    return entry


def summarize(entries: list[dict]) -> dict:
    by_kind: dict[str, dict] = {}
    for e in entries:
        s = by_kind.setdefault(e.get("kind", "?"), {"calls": 0, "usd": 0.0, "inputTokens": 0, "outputTokens": 0})
        s["calls"] += 1
        s["usd"] += e.get("usd", 0.0)
        u = e.get("usage", {})
        s["inputTokens"] += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        s["outputTokens"] += u.get("output_tokens") or 0
    return {
        "totalUsd": sum(e.get("usd", 0.0) for e in entries),
        "calls": len(entries),
        "byKind": by_kind,
    }


def read_ledger(rdir: Path) -> list[dict]:
    path = ledger_path(rdir)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # partial line from an in-flight append
    return entries


def summary_for_run(rdir: Path) -> dict:
    return summarize(read_ledger(rdir))


# ------------------------------------------------- role + window ceilings —
def role_caps() -> dict:
    """Optional per-role ceilings from governance.max_cost_usd.{generation,
    judge,dataset}. Absent keys mean the single run ceiling governs alone."""
    caps = config.get()["governance"].get("max_cost_usd") or {}
    return {k: float(v) for k, v in caps.items()
            if k in ("generation", "judge", "dataset") and isinstance(v, (int, float))}


def breached_role_cap(summary: dict, caps: dict | None) -> str | None:
    for role, cap in (caps or {}).items():
        spent = (summary.get("byKind") or {}).get(role, {}).get("usd", 0.0)
        if spent > cap:
            return f"{role} spend ${spent:.2f} > role cap ${cap:.2f}"
    return None


# ---------------------------------------------- durable cross-run rollup —
def rollup_path() -> Path:
    return config.history_dir() / "spend-ledger.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_rollup(rdir: Path, run_id: str, mode: str, suite: str | None):
    """One durable line per finished spending command — the cross-run ledger
    the daily/weekly windows and `round`/`doctor` totals read. Appended even
    after a kill (the spend happened); idempotent per runId; zero-spend runs
    (all-cached, mocked) record nothing."""
    summary = summary_for_run(rdir)
    spend_kinds = {k: v for k, v in summary["byKind"].items()
                   if k in ("generation", "judge", "dataset")}
    total = sum(v["usd"] for v in spend_kinds.values())
    if not total:
        return None
    path = rollup_path()
    if any(e.get("runId") == run_id for e in _read_jsonl(path)):
        return None
    entry = {"runId": run_id, "mode": mode, "suite": suite,
             "totalUsd": round(total, 6),
             "byKind": {k: round(v["usd"], 6) for k, v in spend_kinds.items()},
             "at": _now()}
    chain.append_chained(path, entry)
    return entry


def blocked_path() -> Path:
    return config.history_dir() / "blocked-spend.jsonl"


def record_blocked(command: str, suite: str | None, estimate_usd: float,
                   ceiling_usd: float, gate: str, basis: str | None = None):
    """Every pre-spend abort persists what it refused to spend — the value
    receipt's 'dollars blocked at the gate' is measured, not folklore."""
    entry = {"command": command, "suite": suite,
             "estimateUsd": round(float(estimate_usd), 6),
             "ceilingUsd": round(float(ceiling_usd), 6),
             "gate": gate, "basis": basis, "at": _now()}
    try:
        chain.append_chained(blocked_path(), entry)
    except OSError:
        pass  # the abort itself must never fail on bookkeeping
    return entry


def savings_totals() -> dict:
    """All-time value receipt: measured generation re-spend avoided by cache
    replays, and estimated spend refused by pre-run gates."""
    hits = saved = 0.0
    runs = config.outputs_dir()
    if runs.is_dir():
        for ledger in runs.glob("*/spend.jsonl"):
            for e in _read_jsonl(ledger):
                if e.get("kind") == "cache_hit":
                    hits += 1
                    saved += e.get("saved_usd") or 0.0
    blocked = _read_jsonl(blocked_path())
    return {"cacheHits": int(hits), "cacheSavedUsd": saved,
            "blockedCount": len(blocked),
            "blockedUsd": sum(e.get("estimateUsd", 0.0) for e in blocked)}


def window_spend(hours: float) -> float:
    """Measured spend in the rolling window ending now — rolling, not
    calendar-day, on purpose: timestamp math is timezone-proof and testable.
    Live (not-yet-rolled-up) run ledgers are included so a crashed or
    in-flight run still counts against the window."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600

    def in_window(at: str | None) -> bool:
        try:
            return datetime.fromisoformat(str(at)).timestamp() >= cutoff
        except ValueError:
            return False

    entries = _read_jsonl(rollup_path())
    total = sum(e.get("totalUsd", 0.0) for e in entries if in_window(e.get("at")))
    rolled = {e.get("runId") for e in entries}
    runs = config.outputs_dir()
    if runs.is_dir():
        for ledger in runs.glob("*/spend.jsonl"):
            if ledger.parent.name in rolled:
                continue
            total += sum(e.get("usd", 0.0) for e in _read_jsonl(ledger)
                         if e.get("kind") in ("generation", "judge", "dataset")
                         and in_window(e.get("at")))
    return total


def exact_cap_hits(entries: list[dict], cap_tokens: int) -> list[dict]:
    """Ledger entries whose output landed exactly on the per-turn token cap —
    the truncation-retry signature that burned $36.67 in the JS campaign
    (incident #5). Surfaced as alerts, never silently."""
    return [e for e in entries if (e.get("usage", {}).get("output_tokens") or 0) == cap_tokens]


def measured_estimates(suite_id: str | None) -> dict | None:
    """Per-trial generation and per-call judge cost measured from the newest
    archived run touching this suite (outputs/history/graded-*.json). Static
    config estimates under-predicted document suites 4x — measured beats
    guessed. Returns None when no archived run with spend data matches.

    Attribution choices (incident #15: workload-matched estimate bases):
    a record matches when the suite appears ANYWHERE in its cases (not just
    the first row); one cases[] row is one executed trial (cell x trial x
    variant), so counting rows stays honest under adaptive-k and multi-suite
    records where the old cases x repeat x variants formula over-counted.
    Per-suite spend is not separable from manifest.spend.byKind, so the
    per-trial gen rate and per-call judge rate are the record-wide averages
    over the ACTUAL trial/call counts — uniform, not suite-weighted."""
    hist = config.history_dir()
    if not hist.exists():
        return None
    for f in sorted(hist.glob("graded-*.json"), reverse=True):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        m = r.get("manifest") or {}
        cases = r.get("cases") or []
        spend = m.get("spend") or {}
        if not spend.get("calls"):
            continue
        if suite_id and not any(c.get("suite") == suite_id for c in cases):
            continue
        trials = len(cases)
        if not trials:  # legacy record without per-case rows
            mode = str(m.get("mode", ""))
            variants = 2 if mode.startswith("compare") or mode == "confirm" else 1
            trials = (m.get("cases") or 0) * (m.get("repeat") or 1) * variants
        gen = (spend.get("byKind") or {}).get("generation")
        if not gen or not trials:
            continue
        judge = (spend.get("byKind") or {}).get("judge")
        return {
            "source": f.name,
            "gen_usd_per_trial": gen["usd"] / trials,
            "judge_usd_per_call": (judge["usd"] / judge["calls"]) if judge and judge.get("calls") else None,
        }
    return None


def measured_calibration_judge_rate() -> dict | None:
    """Per-call judge cost from the newest calibrate run ledger. Calibration
    judges short golden bundles; graded-run judge calls read full agent
    transcripts at ~10x the price, so the two workloads must never share a
    measured estimate (a 12-call ~$0.20 calibration once gated at $2.05)."""
    runs = config.outputs_dir()
    if not runs.exists():
        return None
    for d in sorted(runs.glob("calibrate-*"), reverse=True):
        judge = summary_for_run(d)["byKind"].get("judge")
        if judge and judge.get("calls"):
            return {"source": d.name, "judge_usd_per_call": judge["usd"] / judge["calls"]}
    return None


def estimate_run_usd(gen_calls: int, judge_calls: int, suite_id: str | None,
                     expected_cache_hits: int = 0) -> dict:
    """Pre-run estimate for the budget gate. Measured history when available,
    config fallback otherwise; cache-aware (the JS estimator ignored cache
    replays and over-predicted compare runs 2x, causing false aborts)."""
    measured = measured_estimates(suite_id)
    est_cfg = config.get()["governance"].get("estimate", {})
    gen_unit = measured["gen_usd_per_trial"] if measured else est_cfg.get("gen_usd", 0.5)
    judge_unit = (measured or {}).get("judge_usd_per_call") or est_cfg.get("judge_usd", 0.2)
    billed_gen = max(0, gen_calls - expected_cache_hits)
    return {
        "usd": billed_gen * gen_unit + judge_calls * judge_unit,
        "basis": measured["source"] if measured else "config fallback (no measured history)",
        "gen_unit": gen_unit,
        "judge_unit": judge_unit,
        "cache_hits_assumed": min(expected_cache_hits, gen_calls),
    }

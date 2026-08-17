"""promptfoo extension hook (extensions: file://src/hooks.py:extension_hook).

Runs inside the promptfoo child, so it is the engine's in-run sensor:
  beforeAll  — status banner record
  afterEach  — generation spend to the run ledger (the native provider's
               measured cost), case_end status events the failure-breaker
               watchdog reads, exact-cap and terminal-reason alerts
  afterAll   — spend + row-count trailer

The AUTHORITATIVE budget gate runs in runner.py BEFORE this process spawns
(incident #6 was a gate that never fired); these hooks are measurement."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src import config
from src.utils import cost_tracker, status


def _run_dir() -> Path | None:
    d = os.environ.get("EVAL_RUN_DIR")
    return Path(d) if d else None


def _after_each(context: dict):
    run_dir = _run_dir()
    if run_dir is None:
        return
    result = context.get("result") or {}
    test = context.get("test") or {}
    variables = (test.get("vars") or {})
    case = variables.get("cell") or test.get("description") or "case"
    response = result.get("response") or {}
    metadata = response.get("metadata") or {}

    cached = bool(response.get("cached"))
    cost = response.get("cost") or 0
    token_usage = response.get("tokenUsage") or {}
    cap = int(config.agents()["generation"].get("max_tokens_per_turn", 64000))
    if cached:
        # A replay bills nothing — meter what it AVOIDED (the provider still
        # reports the original call's cost), so cache savings are measured,
        # not folklore. usd stays 0: totals must remain real spend only.
        cost_tracker.record("cache_hit", config.agents()["generation"]["model"],
                            usd=0.0, saved_usd=cost or 0, case=str(case))
    if os.environ.get("EVAL_HTTP_PROVIDER") == "1" and not cached and not cost:
        # An external endpoint bills its owner, not this ledger — the row is
        # recorded so the run's call count stays honest, at usd 0 so no total,
        # window, or measured estimate ever counts money nobody metered.
        cost_tracker.record("cost_unknown", "http", usd=0.0, case=str(case),
                            note="http provider — generation cost unmetered "
                                 "(ceiling governs judge spend only)")
    if cost and not cached:
        gen_model = config.agents()["generation"]["model"]
        cost_tracker.record(
            "generation", gen_model,
            usage={"input_tokens": token_usage.get("prompt"), "output_tokens": token_usage.get("completion")},
            usd=cost, case=str(case),
        )
        if (token_usage.get("completion") or 0) >= cap:
            cost_tracker.record("alert", gen_model, usd=0.0,
                                note=f"output landed on the {cap}-token cap — truncation-retry signature (incident #5)")

    terminal = metadata.get("terminalReason")
    error = result.get("error")
    if terminal and terminal not in ("end_turn", "stop_sequence", "success"):
        status.emit(run_dir, "terminal_reason_alert", case=str(case), note=str(terminal))
    # A truncated output is a failed measurement, not a graded data point: it
    # counts toward the failure breaker (item 21; the original overspend's
    # root cause was silent truncated-output re-billing). Cached replays are
    # skipped — they already alerted (and counted) when live. NEVER retried
    # automatically: a re-run needs an explicit, human-adjusted token limit.
    truncated = not cached and (
        str(terminal) in ("max_tokens", "length", "max_output_tokens")
        or (token_usage.get("completion") or 0) >= cap)
    # promptfoo copies assert-failure reasons into result.error, so `error` alone
    # conflates "judge scored below threshold" with "case blew up" — the breaker
    # killed a valid graded baseline that way (run graded-20260815-133553).
    # failureReason disambiguates: 0=NONE, 1=ASSERT, 2=ERROR. Only ERROR (and,
    # when failureReason is absent, an error with no gradingResult) counts as an
    # infra failure for the breaker; graded fails ride the `passed` field.
    failure_reason = result.get("failureReason")
    if failure_reason is not None:
        infra_error = failure_reason == 2
    else:
        infra_error = bool(error) and result.get("gradingResult") is None
    status.emit(
        run_dir, "case_end",
        case=str(case),
        ok=not infra_error,
        passed=bool(result.get("success")),
        cached=cached,
        truncated=truncated,
        note=(str(error)[:200] if error else None),
    )


def extension_hook(hook_name: str, context: dict):
    run_dir = _run_dir()
    try:
        if hook_name == "beforeAll" and run_dir is not None:
            suite = context.get("suite") or {}
            status.emit(run_dir, "promptfoo_start", note=f"{len(suite.get('tests') or [])} tests")
        elif hook_name == "afterEach":
            _after_each(context)
        elif hook_name == "afterAll" and run_dir is not None:
            spend = cost_tracker.summary_for_run(run_dir)
            results = context.get("results") or []
            status.emit(run_dir, "promptfoo_end",
                        note=f"{len(results)} rows · ${spend['totalUsd']:.2f} measured")
    except Exception as e:  # noqa: BLE001 — observability must never fail the run
        print(f"[hooks] WARN {hook_name} hook failed: {e}", file=sys.stderr)
    return context

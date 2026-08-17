"""Process-group spawn + watchdog for the promptfoo child.

- The child runs in its OWN session (start_new_session) and is killed by
  process GROUP — a wrapper-level pkill once orphaned a promptfoo child that
  billed $12.76 with zero artifacts (incident #2).
- A watchdog thread polls the run's spend ledger and status stream every
  POLL_SECONDS: past the dollar cap it kills the group (incident #7), and a
  run that is only failing gets the failure-rate circuit breaker (incidents
  #1/#5 were 100% failed by minute ten while the dollar watchdog idled).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.utils import cost_tracker, status

POLL_SECONDS = 20


@dataclass
class RunOutcome:
    returncode: int | None = None
    killed_reason: str | None = None
    events: list[str] = field(default_factory=list)


def _kill_group(proc: subprocess.Popen, sig: int):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _breaker_tripped(records: list[dict], cfg: dict) -> str | None:
    """records: afterEach case events with ok True/False, in order.

    Truncated cells count as breaker failures (governance.failure_breaker.
    count_truncations, default true): a run whose outputs are being cut off
    is burning money on unusable measurements. There is no retry here — a
    truncated cell needs a human-adjusted token limit, never a re-bill."""
    count_trunc = cfg.get("count_truncations", True)

    def bad(r: dict) -> bool:
        return r.get("ok") is False or (count_trunc and bool(r.get("truncated")))

    cases = [r for r in records if r.get("ev") == "case_end"]
    n_consec = cfg.get("consecutive_errors", 4)
    tail = cases[-n_consec:]
    if len(tail) == n_consec and all(bad(r) for r in tail):
        return f"{n_consec} consecutive case errors/truncations"
    min_cases = cfg.get("min_cases", 5)
    if len(cases) >= min_cases:
        rate = sum(1 for r in cases if bad(r)) / len(cases)
        if rate > cfg.get("error_rate", 0.5):
            return f"error/truncation rate {rate:.0%} over {len(cases)} cases"
    return None


def progress_note(summary: dict, cap_usd: float, events: list[dict],
                  planned: int | None = None) -> str:
    """One live meter line: running cost vs ceiling, cells done, cache hits.
    Emitted via status (whose stderr mirror is the terminal meter) only when
    the values changed since the last poll."""
    cases = [e for e in events if e.get("ev") == "case_end"]
    cached = sum(1 for e in cases if e.get("cached"))
    spent = summary.get("totalUsd", 0.0)
    pct = f" ({spent / cap_usd * 100:.0f}%)" if cap_usd else ""
    done = f"{len(cases)}/{planned}" if planned else f"{len(cases)}"
    return f"${spent:.2f} of ${cap_usd:.2f}{pct} · {done} cells · {cached} cached"


def run_promptfoo(args: list[str], env: dict, run_dir: Path,
                  cap_usd: float, breaker: dict | None,
                  role_caps: dict | None = None,
                  planned_cells: int | None = None) -> RunOutcome:
    """Spawn `npx promptfoo eval ...`, stream output, enforce the dollar cap
    and failure breaker mid-run. Returns after the whole group is reaped."""
    outcome = RunOutcome()
    proc = subprocess.Popen(
        args,
        cwd=str(run_dir.parents[2]) if len(run_dir.parents) >= 3 else None,
        env=env,
        start_new_session=True,
    )

    forwarded = []

    def forward(signum, _frame):
        _kill_group(proc, signal.SIGTERM)
        threading.Timer(3.0, lambda: _kill_group(proc, signal.SIGKILL)).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        forwarded.append((sig, signal.signal(sig, forward)))

    stop = threading.Event()

    last_progress = [None]

    def watchdog():
        while not stop.wait(POLL_SECONDS):
            summary = cost_tracker.summary_for_run(run_dir)
            events = status.read(run_dir)
            spent = summary["totalUsd"]
            note = progress_note(summary, cap_usd, events, planned_cells)
            if note != last_progress[0]:
                last_progress[0] = note
                status.emit(run_dir, "progress", note=note)
            if cap_usd > 0 and spent > cap_usd:
                outcome.killed_reason = f"spend kill-switch: measured ${spent:.2f} > cap ${cap_usd:.2f}"
            if not outcome.killed_reason and role_caps:
                breach = cost_tracker.breached_role_cap(summary, role_caps)
                if breach:
                    outcome.killed_reason = f"role-cap kill-switch: {breach}"
            if not outcome.killed_reason and breaker:
                trip = _breaker_tripped(events, breaker)
                if trip:
                    outcome.killed_reason = f"failure-rate breaker: {trip}"
            if outcome.killed_reason:
                status.emit(run_dir, "watchdog_kill", note=outcome.killed_reason)
                _kill_group(proc, signal.SIGTERM)
                time.sleep(5)
                _kill_group(proc, signal.SIGKILL)
                return

    t = threading.Thread(target=watchdog, daemon=True)
    t.start()
    try:
        proc.wait()
    finally:
        stop.set()
        _kill_group(proc, signal.SIGKILL)  # parent exiting takes the group with it
        for sig, old in forwarded:
            signal.signal(sig, old)
    outcome.returncode = proc.returncode
    return outcome

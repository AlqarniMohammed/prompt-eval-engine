"""Thin library facade over the CLI internals — deliberately three functions.

Honesty note: the runner mutates process env (EVAL_RUN_DIR, EVAL_SUITE,
EVAL_REPEAT) and takes the same run lock as the CLI, so this facade is NOT
re-entrant and not thread-safe. It exists so evals can be scripted next to
other tooling without shelling out; anything fancier (a pytest plugin, rich
result objects) is a stability promise this closed project declines to make
— run.json is the machine-readable contract.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def run_suite(suite: str, mode: str = "graded", *, repeat: int | None = None,
              max_cost: float | None = None, force: bool = False,
              dry_run: bool = False, filter_first_n: int | None = None) -> int:
    """Run one live mode (baseline/graded/compare/confirm) for a suite.
    Returns the CLI exit code; artifacts land under outputs/runs/ as usual."""
    from src import runner
    if mode not in ("baseline", "graded", "compare", "confirm"):
        raise ValueError(f"unknown mode {mode!r}")
    ns = argparse.Namespace(suite=suite, repeat=repeat, jobs=None,
                            max_cost=max_cost, force=force, resume=None,
                            filter_first_n=filter_first_n, require_clean=False,
                            dry_run=dry_run)
    return runner._run_live(mode, ns)


def verdict(results: str | Path, target: str, *, reason: str | None = None,
            new_evidence: str | None = None) -> dict:
    """Blinded pairwise verdict over a compare results file. Returns the full
    verdict record (record['verdict'] is PROMOTE / REJECT /
    INSUFFICIENT_EVIDENCE). Raises ValueError/PermissionError exactly as the
    CLI would refuse."""
    from src.science.pairwise import compare_verdict
    return compare_verdict(Path(results), target, reason=reason,
                           new_evidence=new_evidence)


def state_of(suite: str | None = None) -> dict:
    """The pipeline view (stages, staleness, next actions) as data — the
    same structure the dashboard and `why` render."""
    from src.dashboard.data import pipeline_view
    view = pipeline_view()
    if suite is not None:
        view = {**view,
                "suites": [s for s in view.get("suites") or [] if s["suite"] == suite]}
    return view

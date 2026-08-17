"""STAGE_DOCS — the single source of truth for pipeline-stage explainer copy.

Served at /api/pipeline, rendered in the dashboard's Pipeline panel, and
drift-checked by tests: every state-machine stage must be documented here,
every `command` must be a real subcommand, and the README must name every
stage. Non-specialist wording on purpose — each entry answers "what does this
step do, what does passing it prove, and why is that worth money/time".
"""

from __future__ import annotations

from src import state

STAGE_DOCS: dict[str, dict] = {
    "validated": {
        "title": "Validated",
        "command": "validate",
        "cost": "$0",
        "what": "Checks every declaration resolves (datasets, prompts, rubrics, golden "
                "fixtures, contract functions), then replays the golden bundles through "
                "the real assertion pipeline with mock models — three sweeps: good outputs "
                "must pass everything, bad outputs must each be caught, and a sabotaged "
                "judge must fail every rubric-wired case.",
        "proves": "The whole measurement pipeline works end to end, and your checks can "
                  "actually catch bad output — before a single model call.",
        "benefit": "Every wiring mistake surfaces here for free instead of inside a paid run.",
    },
    "calibrated": {
        "title": "Calibrated",
        "command": "calibrate",
        "cost": "paid (judge calls only)",
        "what": "The judge scores your known-good, known-bad (and optional borderline) "
                "golden fixtures k times each; every sample must land in the declared "
                "score bands.",
        "proves": "The judge's 1-5 scores mean what the rubric says they mean — high for "
                  "good work, low for bad work, consistently.",
        "benefit": "Without this, judge scores are numbers that look precise and prove "
                   "nothing. No graded run can start on an uncalibrated judge.",
    },
    "baselined": {
        "title": "Baselined",
        "command": "graded",
        "cost": "paid (the main spend)",
        "what": "Runs the production prompt over the full graded matrix — every cell, k "
                "trials each — and the calibrated judge scores every dimension. A small "
                "smoke subset is required first (subset-first gate).",
        "proves": "Where the current prompt actually stands, dimension by dimension, with "
                  "trial-to-trial spread visible.",
        "benefit": "The reference line every improvement is measured against.",
    },
    "compared": {
        "title": "Compared",
        "command": "compare",
        "cost": "paid (~2x a baseline)",
        "what": "Runs the current prompt and your candidate rewrite on the SAME matrix "
                "cells, same trials, same judge — then `verdict` judges the paired outputs "
                "head-to-head, blinded and position-swapped.",
        "proves": "Whether the candidate is actually better, on evidence a biased reader "
                  "can't nudge.",
        "benefit": "Promotions happen on paired verdicts, not on eyeballing two score columns.",
    },
    "promoted": {
        "title": "Promoted",
        "command": "promote",
        "cost": "$0",
        "what": "Copies the candidate over the production prompt — only allowed when the "
                "recorded verdict says PROMOTE.",
        "proves": "The production prompt is exactly the text that won the comparison.",
        "benefit": "No silent prompt edits: what runs in production is what was measured.",
    },
    "confirmed": {
        "title": "Confirmed",
        "command": "confirm",
        "cost": "paid (holdout cells only)",
        "what": "Re-runs the comparison on the held-out matrix cells the optimization "
                "loop never saw.",
        "proves": "The improvement generalizes — it wasn't overfitted to the cells you "
                  "iterated on.",
        "benefit": "A promotion that fails confirmation is reverted, not argued with.",
    },
    # Auxiliary (not in the six-stage display order, but part of the protocol)
    "smoked": {
        "title": "Smoke run",
        "command": "graded",
        "cost": "paid (2 cells)",
        "what": "A tiny subset run (`--filter-first-n 2`) of the full graded "
                "configuration — same content, same models.",
        "proves": "The plumbing works under the real configuration at 2-cell cost.",
        "benefit": "Runtime errors and misconfiguration surface on 2 cells, never on 40 "
                   "(the post-mortem's cheapest lesson, enforced as a gate).",
    },
    "preflight": {
        "title": "Preflight",
        "command": "preflight",
        "cost": "~$0.05",
        "what": "One real generation call with the configured model and caps through the "
                "real provider path, plus one judge call.",
        "proves": "The REAL configuration works — model access, streaming, token caps, "
                  "judge transport.",
        "benefit": "A config that dies in one second costs five cents here instead of a "
                   "whole campaign (incident #9).",
    },
    "verdict": {
        "title": "Verdict",
        "command": "verdict",
        "cost": "paid (2 judge calls per cell)",
        "what": "For each compared cell, the representative current and candidate outputs "
                "are judged head-to-head twice with the anonymous A/B labels swapped; a "
                "side must win both orders or the cell is a tie. Verdicts are idempotent — "
                "repeating one on unchanged inputs needs a recorded --reason.",
        "proves": "The winner wins regardless of presentation order — position bias can't "
                  "decide a promotion, and a REJECT can't be re-rolled until it passes.",
        "benefit": "The honesty core of the whole engine.",
    },
}


def ordered_stages() -> list[str]:
    """The six pipeline stages in display order, then the auxiliary entries."""
    aux = [k for k in STAGE_DOCS if k not in state.STAGES]
    return list(state.STAGES) + aux

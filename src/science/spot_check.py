"""Human spot-check of judge verdicts — zero API calls by construction.

Calibration proves the judge was sane on golden fixtures at one moment; it
does not prove it stays sane on real, messy outputs over time. This closes
that gap: sample judged cells from a finished run's ARTIFACTS
(judge-evidence.jsonl joined to results.json), collect the human's
per-dimension pass/fail BEFORE revealing the judge's scores, and keep a
rolling human↔judge agreement rate in the hash-chained
outputs/history/spot-checks.jsonl.

Sampling is deterministic hash-ranked (seeded by runId), spread across
cells — cherry-picking the interesting ones is exactly what the RUNBOOK
protocol forbids.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.reports.parse_results import rows_of
from src.utils import chain
from src.utils import rubric as rubric_lib


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def checks_path() -> Path:
    return config.history_dir() / "spot-checks.jsonl"


def sample_cells(run_dir: Path, n: int) -> list[dict]:
    """Deterministic sample of judged cells with their output text.
    One sample per (cell, trial, variant); unseen cells are preferred over a
    second trial of an already-sampled cell (spread across the matrix)."""
    evidence = [e for e in _read_jsonl(run_dir / "judge-evidence.jsonl")
                if isinstance(e.get("scores"), dict)]
    outputs = {}
    for r in rows_of(run_dir / "results.json") if (run_dir / "results.json").exists() else []:
        v = (r.get("testCase") or {}).get("vars") or {}
        out = (r.get("response") or {}).get("output")
        outputs[(v.get("cell"), (v.get("trial")), v.get("promptVariant"))] = \
            out if isinstance(out, str) else ""
    run_id = run_dir.name
    seen_keys = set()
    candidates = []
    for e in evidence:
        v = e.get("vars") or {}
        key = (e.get("cell"), v.get("trial"), v.get("promptVariant"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rank = hashlib.sha256(f"{run_id}|{key}".encode()).hexdigest()
        candidates.append({"suite": e.get("suite"), "cell": e.get("cell"),
                           "trial": v.get("trial"), "variant": v.get("promptVariant"),
                           "scores": e["scores"], "judgeModel": e.get("judgeModel"),
                           "judge": e.get("judge") or {},
                           "output": outputs.get(key, ""), "rank": rank})
    candidates.sort(key=lambda c: c["rank"])
    picked, seen_cells = [], set()
    for prefer_new_cell in (True, False):
        for c in candidates:
            if len(picked) >= n:
                return picked
            if c in picked:
                continue
            if prefer_new_cell and c["cell"] in seen_cells:
                continue
            picked.append(c)
            seen_cells.add(c["cell"])
    return picked


def spot_check(run_dir: Path, n: int, answer_fn, log=print) -> dict:
    """Run the interactive protocol. answer_fn(dim_name, sample) -> bool is
    the human's pass/fail per dimension, asked BEFORE the judge's score is
    revealed. Labels are chained into spot-checks.jsonl."""
    samples = sample_cells(run_dir, n)
    if not samples:
        raise ValueError(f"no judged cells with evidence in {run_dir} — "
                         "spot-check needs a finished graded-tier run")
    threshold = config.get().get("graded", {}).get("pass_threshold", 3)
    agree = total = 0
    per_dim: dict[str, list[int]] = {}
    for i, s in enumerate(samples, start=1):
        rubric = rubric_lib.for_suite(s["suite"])
        rubric_sha = _rubric_sha(s["suite"])
        log("─" * 72)
        log(f"[{i}/{len(samples)}] {s['suite']} · cell {s['cell']}"
            + (f" · trial {s['trial']}" if s.get("trial") is not None else "")
            + (f" · {s['variant']}" if s.get("variant") else ""))
        log("─" * 72)
        log(s["output"] or "(no output text captured for this row)")
        log("")
        for d in rubric["dimensions"]:
            dim = d["name"]
            log(f"{dim}: {d['measures']}")
            log("  anchors: " + " · ".join(f"{lvl}={d['anchors'][lvl]}" for lvl in "51"))
            human = bool(answer_fn(dim, s))
            judge_score = s["scores"].get(dim)
            judge_pass = isinstance(judge_score, (int, float)) and judge_score >= threshold
            same = human == judge_pass
            agree += same
            total += 1
            per_dim.setdefault(dim, []).append(int(same))
            log(f"  judge scored {judge_score} → {'pass' if judge_pass else 'fail'} · "
                f"you said {'pass' if human else 'fail'} → {'AGREE' if same else 'DISAGREE'}")
            chain.append_chained(checks_path(), {
                "runId": run_dir.name, "suite": s["suite"], "cell": s["cell"],
                "trial": s.get("trial"), "dim": dim,
                "human": human, "judgeScore": judge_score, "agree": same,
                "judgeModel": s.get("judgeModel"), "rubricSha": rubric_sha,
                "at": datetime.now(timezone.utc).isoformat(),
            })
    return {"labels": total, "agreement": (agree / total) if total else None,
            "perDim": {d: sum(v) / len(v) for d, v in per_dim.items()}}


def _rubric_sha(suite_id: str) -> str | None:
    from src import state
    suite = config.suite_by_id(suite_id) or {}
    return state.sha256_file(config.resolve(suite["rubric"])) if suite.get("rubric") else None


def rolling_agreement(suite_id: str | None = None) -> dict:
    """Cumulative human↔judge agreement, filtered to labels earned under the
    CURRENT judge model + rubric (a changed judge starts a fresh count)."""
    judge_model = config.agents()["judge"]["model"]
    entries = []
    for e in _read_jsonl(checks_path()):
        if e.get("judgeModel") != judge_model:
            continue
        if suite_id and e.get("suite") != suite_id:
            continue
        if suite_id is None or e.get("rubricSha") == _rubric_sha(suite_id):
            entries.append(e)
    n = len(entries)
    rate = (sum(1 for e in entries if e.get("agree")) / n) if n else None
    return {"labels": n, "agreement": rate}


def agreement_warning() -> str | None:
    """WARN line for round/doctor when the rolling rate is below the floor
    with enough labels to mean something (RUNBOOK: ≥90% at ~30 labels)."""
    floor = config.get().get("graded", {}).get("calibration", {}).get(
        "min_human_agreement", 0.9)
    rolling = rolling_agreement()
    if rolling["labels"] >= 30 and rolling["agreement"] is not None \
            and rolling["agreement"] < float(floor):
        return (f"judge↔human agreement {rolling['agreement']:.0%} over "
                f"{rolling['labels']} labels is below the "
                f"{float(floor):.0%} floor — fix rubric anchors and recalibrate "
                "(never bend your labels toward the judge)")
    return None

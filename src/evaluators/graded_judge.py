"""The graded tier's promptfoo assert: one judge call per trial, all
dimensions via namedScores, deterministic-contract context in metadata, and
an append to the run's judge-evidence.jsonl — a sidecar we own, so reporting
never depends on promptfoo preserving metadata across versions.

A judge that stays invalid after one re-ask is a JUDGE-ERROR row: never a
fabricated score; reporting treats it as "no measurement", not a low score."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.evaluators.llm_judge import judge_bundle


def _sidecar_append(record: dict):
    run_dir = os.environ.get("EVAL_RUN_DIR")
    directory = Path(run_dir) if run_dir else config.outputs_root()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "judge-evidence.jsonl").open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **record}) + "\n")


def _axis_vars(variables: dict) -> dict:
    return {k: v for k, v in variables.items()
            if k.startswith("axis") or k in ("holdout", "trial", "promptVariant")}


def graded_judge_assert(output, context):
    variables = (context or {}).get("vars") or {}
    suite_id = variables.get("suite")
    if not suite_id:
        return {"pass": False, "score": 0, "reason": "JUDGE-ERROR: case has no suite var"}
    cell = variables.get("cell") or variables.get("golden") or "case"

    try:
        result = judge_bundle(suite_id, output, variables)
    except Exception as e:  # noqa: BLE001 — every judge failure is a recorded no-measurement
        _sidecar_append({"suite": suite_id, "cell": cell, "judgeError": str(e), "vars": _axis_vars(variables)})
        return {
            "pass": False, "score": 0,
            "reason": f"JUDGE-ERROR (after one re-ask): {e}",
            "metadata": {"judgeError": str(e)},
        }

    scores = result["scores"]
    threshold = config.get().get("graded", {}).get("pass_threshold", 3)
    values = list(scores.values())
    mean = sum(values) / len(values)
    _sidecar_append({
        "suite": suite_id, "cell": cell, "judgeModel": result["judge_model"],
        "scores": scores, "judge": result["json"], "vars": _axis_vars(variables),
    })
    top_issue = str(result["json"].get("top_issue", ""))[:120]
    tag = result["json"].get("top_issue_tag")
    passed = all(s >= threshold for s in values)
    # Failing rows carry the taxonomy tag as a machine-greppable [tag] reason
    # prefix — the report and dashboard aggregate these.
    prefix = f"[{tag}] " if (tag and not passed) else ""
    return {
        "pass": passed,
        "score": mean / 5,
        "reason": prefix + " · ".join(f"{k} {v}" for k, v in scores.items()) + f" | {top_issue}",
        "namedScores": scores,
        "metadata": {"judge": result["json"], "judgeModel": result["judge_model"],
                     "suite": suite_id, "cell": cell},
    }

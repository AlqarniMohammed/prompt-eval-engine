"""Pin a failing cell into the permanent regression set.

The most valuable test cases are the ones that once failed and were fixed;
left in the general matrix they get regenerated away and the same bug
returns. `pin` copies the cell's case VERBATIM from the generated graded
matrix (the faithful source of what actually ran) into
datasets/regression/<suite>.yaml, stamped `regression: "true"`. Regression
cases run in every graded/compare/confirm pass, and any failure among them
fails the run loudly regardless of the aggregate score.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src import config
from src.utils import fsatomic


def pin_cell(run_dir: Path, cell: str, note: str | None = None) -> dict:
    """Copy the graded-matrix case for `cell` (as run in run_dir) into the
    suite's regression file. Returns {suite, cell, path}."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"no manifest.json in {run_dir} — is this a run directory?")
    manifest = json.loads(manifest_path.read_text())

    results = run_dir / "results.json"
    rows = []
    if results.exists():
        data = json.loads(results.read_text())
        rows = (data.get("results") or {}).get("results") or []
    row = next((r for r in rows
                if ((r.get("testCase") or {}).get("vars") or {}).get("cell") == cell), None)
    if row is None:
        raise ValueError(f'cell "{cell}" not found in {run_dir.name}/results.json')
    suite_id = ((row.get("testCase") or {}).get("vars") or {}).get("suite") or manifest.get("suite")
    if not suite_id:
        raise ValueError(f'cell "{cell}" carries no suite id and the manifest has none')

    matrix = config.graded_dir() / f"{suite_id}.yaml"
    if not matrix.exists():
        raise ValueError(f"no generated matrix at {config.display_path(matrix)} — "
                         "the pinned case must come from the matrix that actually ran")
    case = next((c for c in yaml.safe_load(matrix.read_text()) or []
                 if (c.get("vars") or {}).get("cell") == cell), None)
    if case is None:
        raise ValueError(f'cell "{cell}" is not in the current matrix '
                         f"{config.display_path(matrix)} (regenerated since the run?)")

    pinned = {**case, "vars": {**(case.get("vars") or {}),
                               "cell": f"{cell}·pinned",
                               "regression": "true"}}
    pinned["vars"].pop("holdout", None)  # a regression case is never held out
    pinned["description"] = (f"[regression] {case.get('description') or cell} "
                             f"(pinned from {manifest.get('runId')})")

    out = config.regression_dir() / f"{suite_id}.yaml"
    existing = yaml.safe_load(out.read_text()) if out.exists() else []
    existing = existing or []
    if any((c.get("vars") or {}).get("cell") == pinned["vars"]["cell"] for c in existing):
        raise ValueError(f'cell "{cell}" is already pinned for suite {suite_id}')
    existing.append(pinned)
    header = (f"# Permanent regression set for suite {suite_id} — cases pinned from\n"
              f"# failing runs via `prompt-eval pin`. They run in EVERY graded-tier\n"
              f"# pass (graded/compare/confirm) and any failure fails the run loudly.\n"
              f"# Last pin: {datetime.now(timezone.utc).isoformat()}"
              + (f" — {note}" if note else "") + "\n")
    fsatomic.write_text_atomic(out, header + yaml.safe_dump(existing, sort_keys=False))
    return {"suite": suite_id, "cell": pinned["vars"]["cell"], "path": out}

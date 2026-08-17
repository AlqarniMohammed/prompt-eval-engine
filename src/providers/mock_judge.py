"""Offline grading provider for llm-rubric asserts.
  MOCK_JUDGE=pass (default): every rubric passes — offline validation
    exercises only the deterministic assertions.
  MOCK_JUDGE=fail: canned failing verdict — validate step 4 uses this to
    prove every rubric-bearing case is actually wired to a judge (a rubric
    that can't fail offline is a rubric that silently never graded)."""

from __future__ import annotations

import json
import os


def call_api(prompt: str, options: dict, context: dict) -> dict:
    if os.environ.get("MOCK_JUDGE") == "fail":
        return {"output": json.dumps({
            "pass": False, "score": 0,
            "reason": "MOCK_JUDGE=fail canned verdict (offline rubric-wiring proof)",
        })}
    return {"output": json.dumps({
        "pass": True, "score": 1,
        "reason": "offline run: rubric asserts are skipped",
    })}

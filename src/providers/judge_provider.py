"""Live grading provider for the binary tier's llm-rubric asserts — the
configured judge agent (never the generator), with every call on the spend
ledger. Referenced as defaultTest.options.provider in the live configs."""

from __future__ import annotations


from src.evaluators.llm_judge import judge_call


def call_api(prompt: str, options: dict, context: dict) -> dict:
    try:
        return {"output": judge_call(prompt)}
    except Exception as e:  # noqa: BLE001 — provider contract: error string, not a crash
        return {"error": f"judge call failed: {e}"}

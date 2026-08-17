"""Dataset-case generator: a CHEAP model simulates ordinary users.

The generator writes the prompts that go INTO the system under test — it is
not being judged, so intelligence buys nothing here. What matters is realism:
a strong model "helpfully" writes best-practice, fully-specified prompts no
real user would type, and an eval fed legendary inputs measures the wrong
thing. The realism charter below is therefore part of the engine, not a tip.

Output is a DRAFT file under datasets/generated/ — never auto-wired into a
suite. A human reviews, merges the keepers into the suite's graded spec, and
runs `prompt-eval matrix`. Human review is the quality gate that costs
nothing.

MOCK_DATASET=canned|malformed|malformed-once replaces the live call so the
whole path is provable offline for free (mirrors MOCK_GRADED_JUDGE).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import yaml

from src import config
from src.evaluators.llm_judge import extract_json_object, _strip_fences
from src.utils import model_client

REALISM_CHARTER = """Write each prompt exactly as an ORDINARY, busy user would type it:
- terse and underspecified; assume context the system doesn't have
- no role-priming ("act as..."), no output-format requests, no rubrics
- occasional ambiguity, hedging, or a typo — real users are imperfect
- vary tone: some polite, some rushed, some frustrated
- do NOT write best-practice prompts. If a prompt reads like a prompt-
  engineering example, it is wrong for this dataset."""

_mock_calls = 0


def _mock_reply(mode: str, count: int) -> str:
    global _mock_calls
    _mock_calls += 1
    cases = json.dumps({"cases": [
        {"id": f"draft-{i + 1}", "probe": f"mock ordinary-user prompt {i + 1}"}
        for i in range(count)
    ]})
    if mode == "canned":
        return "```json\n" + cases + "\n```"
    if mode == "malformed":
        return "Sure! Here are some great prompts you could use."
    if mode == "malformed-once":
        return '{"cases": broken' if _mock_calls == 1 else cases
    raise ValueError(f'unknown MOCK_DATASET mode "{mode}"')


def _context_for(suite_id: str) -> str:
    """The suite's production prompt + a couple of existing probes, so the
    generator knows the system being fed — few-shot for topic, not style."""
    parts = []
    spec_path = config.specs_dir() / f"{suite_id}.yaml"
    if spec_path.exists():
        spec = yaml.safe_load(spec_path.read_text())
        prompt_file = (spec.get("base") or {}).get("promptFile")
        if prompt_file:
            prod = config.resolve(config.get()["prompts"]["production_dir"]) / prompt_file
            if prod.exists():
                parts.append("The system prompt under test:\n---\n" + prod.read_text()[:4000] + "\n---")
        probes = [v.get("probe") for axis in (spec.get("axes") or {}).values()
                  for v in axis if isinstance(v, dict) and v.get("probe")][:3]
        if probes:
            parts.append("Existing example inputs (match their TOPIC, not their polish):\n"
                         + "\n".join(f"- {p}" for p in probes))
    return "\n\n".join(parts) or f"The system under test is suite {suite_id}."


def _build_prompt(suite_id: str, count: int) -> str:
    return (
        f"You are simulating end users of an LLM-powered system. Generate {count} "
        "realistic user inputs for testing it.\n\n"
        f"{_context_for(suite_id)}\n\n"
        f"{REALISM_CHARTER}\n\n"
        'Reply with ONLY a JSON object: {"cases": [{"id": "<kebab-case-slug>", '
        f'"probe": "<the user\'s message>"}}, ...]}} with exactly {count} cases, '
        "no fences, no prose."
    )


def _validate_reply(text: str, count: int) -> list[dict]:
    try:
        reply = json.loads(extract_json_object(_strip_fences(text)))
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e}): {str(text)[:120]}")
    cases = reply.get("cases") if isinstance(reply, dict) else None
    if not isinstance(cases, list) or len(cases) != count:
        raise ValueError(f'expected {count} entries under "cases", got '
                         f"{len(cases) if isinstance(cases, list) else 'none'}")
    seen = set()
    for c in cases:
        if not isinstance(c.get("id"), str) or not isinstance(c.get("probe"), str) \
                or not c["id"] or not c["probe"]:
            raise ValueError("every case needs non-empty string id + probe")
        if c["id"] in seen:
            raise ValueError(f'duplicate case id "{c["id"]}"')
        seen.add(c["id"])
    return [{"id": c["id"], "probe": c["probe"]} for c in cases]


def generate_cases(suite_id: str, count: int, out_dir=None) -> dict:
    """One dataset-model call → validated draft YAML. Re-asks exactly once on
    an invalid reply, then raises (mirrors judge_bundle)."""
    mock_mode = os.environ.get("MOCK_DATASET")
    dataset_agent = config.get()["agents"].get("dataset")
    if dataset_agent is None:
        raise config.ConfigError("agents.dataset is not configured — gen-cases needs it")
    base_prompt = _build_prompt(suite_id, count)

    cases = None
    last_error = None
    for attempt in range(2):
        prompt = base_prompt if attempt == 0 else (
            f"{base_prompt}\n\nYour previous reply was rejected: {last_error}. "
            "Reply again with ONLY the JSON object described above."
        )
        reply = _mock_reply(mock_mode, count) if mock_mode else \
            model_client.call(dataset_agent, "dataset", prompt)
        try:
            cases = _validate_reply(reply, count)
            break
        except ValueError as e:
            last_error = str(e)
    if cases is None:
        raise RuntimeError(f"dataset generator invalid after one re-ask: {last_error}")

    out_dir = out_dir or config.graded_dir().parent / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{suite_id}-{stamp}.yaml"
    model_id = f"mock:{mock_mode}" if mock_mode else dataset_agent["model"]
    header = (
        f"# DRAFT — generated by gen-cases ({model_id}) on {stamp}. NOT wired into\n"
        f"# any suite. Review for realism and correctness, merge the keepers into an\n"
        f"# axis in the suite's spec under graded specs, then run:\n"
        f"#   prompt-eval matrix --suite {suite_id}\n"
    )
    out.write_text(header + yaml.safe_dump(
        {"suite": suite_id, "candidates": cases},
        sort_keys=False, allow_unicode=True, width=88))
    return {"out": out, "cases": cases, "model": model_id}

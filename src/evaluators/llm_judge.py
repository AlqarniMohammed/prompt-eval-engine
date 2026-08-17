"""The judge client + graded judge core.

judge_call()    — one call on the configured judge agent (never the
                  generator; judge==gen is hard-blocked in config.agents()),
                  dispatched by model prefix through model_client: anthropic
                  by default, openai: via the cross-judge extra. Free-only
                  overload retries, run-ledger spend recording, and the >16k
                  output cap that keeps JSON from truncating mid-object all
                  live there.
judge_bundle()  — builds the rubric prompt, calls the judge, strips fences,
                  schema-validates the dimensions JSON, re-asks exactly once,
                  then RAISES — a fabricated score is worse than no score.

MOCK_GRADED_JUDGE=good|bad|malformed|malformed-once|band replaces the live
judge with canned replies so the whole path is provable offline for free.
`band` scores each golden from its OWN label (pass→5s, fail→2s, mid→3s) so an
offline calibration can go GREEN — it proves wiring and band arithmetic,
never judge quality."""

from __future__ import annotations

import json
import os
import re

from src import config
from src.utils import model_client, rubric as rubric_lib

_mock_call_counts: dict[str, int] = {}


def _mock_reply(mode: str, rubric: dict, call_key: str) -> str:
    n = _mock_call_counts.get(call_key, 0) + 1
    _mock_call_counts[call_key] = n

    def dims(score: int) -> str:
        return json.dumps({
            "dimensions": {
                d["name"]: {"evidence": "mock evidence line", "reasoning": "mock reasoning", "score": score}
                for d in rubric["dimensions"]
            },
            "top_issue": "mock top issue",
            "suggested_prompt_fix": "mock suggested fix",
        })

    if mode == "good":
        return "```json\n" + dims(5) + "\n```"  # fenced on purpose
    if mode == "bad":
        return dims(1)
    if mode == "malformed":
        return "I think it deserves a solid 4/5 overall!"
    if mode == "malformed-once":
        return '{"dimensions": broken' if n == 1 else dims(4)
    if mode == "band":
        # Scores from the golden fixture's OWN label — call_key carries the
        # golden filename during calibration ("<suite>|<golden>/<file>#<i>").
        # Proves wiring and band arithmetic, never judge quality; non-golden
        # calls (graded cells) score a flat passing 4.
        base = call_key.rsplit("/", 1)[-1]
        score = (5 if base.startswith("pass") else
                 2 if base.startswith("fail") else
                 3 if base.startswith("mid") else 4)
        return dims(score)
    raise ValueError(f'unknown MOCK_GRADED_JUDGE mode "{mode}"')


def judge_call(prompt: str) -> str:
    """One live judge call, dispatched by model prefix (anthropic default,
    openai: via the cross-judge extra). Free-only retries, ledger recording,
    and the token-cap alert live in model_client."""
    config.load_env_file()
    return model_client.call(config.agents()["judge"], "judge", prompt)


def _strip_fences(text: str) -> str:
    text = str(text).strip()
    text = re.sub(r"^```[a-z]*\s*\n?", "", text, flags=re.I)
    return re.sub(r"\n?```\s*$", "", text).strip()


def extract_json_object(text: str) -> str:
    """Judges occasionally append prose after the JSON; scan for the balanced
    object so one chatty reply doesn't kill a whole run."""
    start = text.find("{")
    if start == -1:
        return text
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = in_string
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def validate_reply(text: str, rubric: dict) -> dict:
    try:
        reply = json.loads(extract_json_object(_strip_fences(text)))
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e}): {str(text)[:120]}")
    if not isinstance(reply, dict) or not isinstance(reply.get("dimensions"), dict):
        raise ValueError('missing "dimensions" object')
    for d in rubric["dimensions"]:
        entry = reply["dimensions"].get(d["name"])
        if not entry:
            raise ValueError(f'dimension "{d["name"]}" missing from reply')
        score = entry.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
            raise ValueError(f'dimension "{d["name"]}" score must be an integer 1-5, got {score!r}')
        if not isinstance(entry.get("evidence"), str) or not isinstance(entry.get("reasoning"), str):
            raise ValueError(f'dimension "{d["name"]}" needs string evidence + reasoning')
    if not isinstance(reply.get("top_issue"), str) or not isinstance(reply.get("suggested_prompt_fix"), str):
        raise ValueError("missing top_issue / suggested_prompt_fix strings")
    # The taxonomy tag is OPTIONAL and never fails a paid reply: unknown
    # values coerce to "other", absence stays absent (older judge replies).
    tag = reply.get("top_issue_tag")
    if tag is not None:
        reply["top_issue_tag"] = tag if tag in rubric_lib.taxonomy() else "other"
    return reply


def judge_bundle(suite_id: str, bundle: str, variables: dict | None = None) -> dict:
    """Judge one bundle against a suite's rubric. Returns {json, scores,
    judge_model, rubric}; raises after exactly one re-ask on invalid replies."""
    variables = variables or {}
    rubric = rubric_lib.for_suite(suite_id)
    mock_mode = os.environ.get("MOCK_GRADED_JUDGE")
    # A mocked judge must never masquerade as the configured model: the
    # calibration state gate matches on judge_model, and a mock record
    # claiming the real judge would open the graded gate for free.
    judge_model = f"mock:{mock_mode}" if mock_mode else config.agents()["judge"]["model"]
    call_key = f"{suite_id}|{variables.get('cell') or variables.get('golden') or 'case'}"
    base_prompt = rubric_lib.build_judge_prompt(rubric, suite_id, bundle)

    reply_json = None
    last_error = None
    for attempt in range(2):
        prompt = base_prompt if attempt == 0 else (
            f"{base_prompt}\n\nYour previous reply was rejected: {last_error}.\n"
            "Reply again with ONLY the JSON object described above — no fences, no prose. "
            'Escape every double quote inside string values as \\" (quoting the bundle '
            "verbatim is the usual cause of this rejection), or use single quotes in prose."
        )
        reply_text = _mock_reply(mock_mode, rubric, call_key) if mock_mode else judge_call(prompt)
        try:
            reply_json = validate_reply(reply_text, rubric)
            break
        except ValueError as e:
            last_error = str(e)
    if reply_json is None:
        raise RuntimeError(f"judge invalid after one re-ask: {last_error}")
    scores = {d["name"]: reply_json["dimensions"][d["name"]]["score"] for d in rubric["dimensions"]}
    return {"json": reply_json, "scores": scores, "judge_model": judge_model, "rubric": rubric}

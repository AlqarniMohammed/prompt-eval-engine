"""Loads suite datasets declared in eval_config.yaml, stamping a `suite` var
on every case. Suite selection resolves through the config table
(EVAL_SUITE=<id>), never through filename-prefix parsing.

Multi-turn cases (`messages:` on a case) are SIMULATION, honestly scoped: the
agent-sdk provider takes a single free-text prompt (internal loop), so prior
turns are rendered into the prompt as a labeled simulated-transcript block —
never replayed as real message-array turns. Real multi-turn requires an
http-provider suite whose endpoint accepts a message array."""

from __future__ import annotations

import os

import yaml

from src import config


def message_problems(messages) -> list[str]:
    """Structural problems with a case's `messages:` list. Empty = valid.
    Rules: {role, content} turns, roles strictly alternating, the LAST turn
    must be user — it is the live request the reply is graded on."""
    if not isinstance(messages, list) or not messages:
        return ["messages: must be a non-empty YAML list of {role, content} turns"]
    problems = []
    for i, m in enumerate(messages):
        role = m.get("role") if isinstance(m, dict) else None
        content = m.get("content") if isinstance(m, dict) else None
        if role not in ("user", "assistant"):
            problems.append(f"messages[{i}]: role must be user or assistant (got {role!r})")
        if not isinstance(content, str) or not content.strip():
            problems.append(f"messages[{i}]: content must be a non-empty string")
    if problems:
        return problems
    for i in range(1, len(messages)):
        if messages[i]["role"] == messages[i - 1]["role"]:
            problems.append(f"messages[{i}]: roles must alternate "
                            f"(two {messages[i]['role']} turns in a row)")
    if messages[-1]["role"] != "user":
        problems.append("messages: the last turn must be user (it is the live request)")
    return problems


def render_messages(messages: list[dict]) -> str:
    """The labeled simulated-transcript block a `messages:` case becomes."""
    lines = ["[SIMULATED CONVERSATION SO FAR — single-prompt simulation, not real multi-turn]"]
    for m in messages[:-1]:
        lines.append(f"{m['role']}: {m['content'].strip()}")
    lines.append("[END OF SIMULATED CONVERSATION]")
    lines.append("")
    lines.append("The user's next message:")
    lines.append(messages[-1]["content"].strip())
    return "\n".join(lines)


def adopt_messages(case: dict, where: str) -> dict:
    """Convert a case's `messages:` into vars.simulatedTranscript (a plain
    string var — a list under vars would trigger promptfoo's cartesian-product
    explosion). Raises on structural problems; `validate` catches them free."""
    msgs = case.get("messages")
    if msgs is None:
        return case
    problems = message_problems(msgs)
    if problems:
        raise ValueError(f"{where}: " + "; ".join(problems))
    variables = case.get("vars") or {}
    if variables.get("probe"):
        raise ValueError(f"{where}: messages: and vars.probe are mutually "
                         "exclusive — the final user turn IS the probe")
    return {**{k: v for k, v in case.items() if k != "messages"},
            "vars": {**variables, "simulatedTranscript": render_messages(msgs)}}


def load_cases(suite_filter: str | None = None) -> list[dict]:
    suite_filter = suite_filter or os.environ.get("EVAL_SUITE")
    matched = [s for s in config.suites() if not suite_filter or s["id"] == suite_filter]
    if suite_filter and not matched:
        known = ", ".join(s["id"] for s in config.suites())
        raise ValueError(f'EVAL_SUITE="{suite_filter}" matches no suite — known ids: {known}')
    cases = []
    for suite in matched:
        data = yaml.safe_load(config.resolve(suite["file"]).read_text()) or []
        if not isinstance(data, list):
            raise ValueError(f'{suite["file"]} must be a YAML list of test cases')
        for c in data:
            c = adopt_messages(c, f'{suite["id"]}: {c.get("description") or "case"}')
            cases.append({**c, "vars": {**(c.get("vars") or {}), "suite": suite["id"]}})
    return cases


def load_graded_cases(suite_filter: str | None = None, holdout: str | None = None) -> list[dict]:
    """Generated matrix cases from datasets/graded/. Holdout cells are
    excluded by default — they are spent only on post-promotion confirmation
    (holdout='only') or a full re-baseline (holdout='include')."""
    suite_filter = suite_filter or os.environ.get("EVAL_SUITE")
    holdout = holdout or os.environ.get("EVAL_HOLDOUT", "exclude")
    graded_dir = config.graded_dir()
    matched = [s for s in config.suites() if s.get("rubric")]
    if suite_filter:
        matched = [s for s in matched if s["id"] == suite_filter]
    if not matched:
        raise ValueError(
            f'suite "{suite_filter}" not found or has no rubric: entry' if suite_filter
            else "no suites declare a rubric: — nothing to grade"
        )
    cases = []
    for suite in matched:
        path = graded_dir / f'{suite["id"]}.yaml'
        if not path.exists():
            raise FileNotFoundError(f'no generated matrix for suite "{suite["id"]}" — run: prompt-eval matrix')
        for c in yaml.safe_load(path.read_text()) or []:
            c = adopt_messages(c, f'{suite["id"]}: {(c.get("vars") or {}).get("cell") or "cell"}')
            cases.append({**c, "vars": {**(c.get("vars") or {}), "suite": suite["id"]}})

    def keep(c: dict) -> bool:
        is_holdout = str(c["vars"].get("holdout")) == "true"
        if holdout == "only":
            return is_holdout
        if holdout == "include":
            return True
        return not is_holdout

    kept = [c for c in cases if keep(c)]
    # Pinned regression cases ride along in EVERY graded-tier run — including
    # confirm's holdout-only pass: a fixed failure must never return, and a
    # holdout that ignores regressions would let one back in at promotion.
    for suite in matched:
        reg = config.regression_dir() / f'{suite["id"]}.yaml'
        if not reg.exists():
            continue
        for c in yaml.safe_load(reg.read_text()) or []:
            c = adopt_messages(c, f'regression {suite["id"]}: {(c.get("vars") or {}).get("cell") or "cell"}')
            kept.append({**c, "vars": {**(c.get("vars") or {}),
                                       "suite": suite["id"], "regression": "true"}})
    # `monitor` restricts a run to its fixed canary subset (set by the
    # runner, never by hand): the tiniest honest drift check.
    canary = os.environ.get("EVAL_MONITOR_CELLS")
    if canary:
        wanted = set(canary.split(","))
        kept = [c for c in kept if c["vars"].get("cell") in wanted]
    return kept

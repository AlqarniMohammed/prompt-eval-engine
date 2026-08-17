"""Reusable deterministic contract checks (promptfoo python asserts).

Users write the same checks repeatedly — JSON validity, required headings,
length bounds, forbidden phrases. These ship tested so a contracts module is
three lines, not thirty. Reference them from a dataset case:

    assert:
      - { type: python, value: "file://src/contracts_lib.py:json_valid" }

Parameterization comes from the CASE's vars (file:// refs can't close over
arguments): `required_headings`, `min_chars`/`max_chars`,
`forbidden_phrases`, `regex_required` — comma-separated strings where a
list is implied (promptfoo explodes YAML arrays into a cartesian product).

Failure reasons carry a machine-greppable [tag] prefix from the failure
taxonomy, so contract failures aggregate in the report alongside the
judge's tags.
"""

from __future__ import annotations

import json as _json
import re as _re


def _vars(context) -> dict:
    return (context or {}).get("vars") or {}


def _split(value) -> list[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _ok(reason: str) -> dict:
    return {"pass": True, "score": 1.0, "reason": reason}


def _fail(tag: str, reason: str) -> dict:
    return {"pass": False, "score": 0.0, "reason": f"[{tag}] {reason}"}


def json_valid(output, context):
    """The output (or its first fenced block) must parse as JSON."""
    text = str(output).strip()
    fenced = _re.search(r"```(?:json)?\s*\n(.*?)```", text, flags=_re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        _json.loads(candidate)
        return _ok("valid JSON")
    except _json.JSONDecodeError as e:
        return _fail("format", f"not valid JSON: {e}")


def required_headings(output, context):
    """Every heading named in vars.required_headings (comma-separated) must
    appear as a markdown heading line."""
    wanted = _split(_vars(context).get("required_headings"))
    if not wanted:
        return _fail("format", "vars.required_headings is empty — nothing to check")
    text = str(output)
    headings = {m.group(1).strip().lower()
                for m in _re.finditer(r"^#{1,6}\s+(.+)$", text, flags=_re.M)}
    missing = [h for h in wanted if h.lower() not in headings]
    if missing:
        return _fail("missing-content", f"missing heading(s): {', '.join(missing)}")
    return _ok(f"all {len(wanted)} required headings present")


def length_between(output, context):
    """len(output) within [vars.min_chars, vars.max_chars] (either optional)."""
    v = _vars(context)
    n = len(str(output))
    lo = int(v["min_chars"]) if v.get("min_chars") else None
    hi = int(v["max_chars"]) if v.get("max_chars") else None
    if lo is None and hi is None:
        return _fail("format", "neither vars.min_chars nor vars.max_chars set")
    if lo is not None and n < lo:
        return _fail("missing-content", f"{n} chars < min {lo}")
    if hi is not None and n > hi:
        return _fail("format", f"{n} chars > max {hi}")
    return _ok(f"length {n} within bounds")


def forbidden_phrases(output, context):
    """None of vars.forbidden_phrases (comma-separated, case-insensitive)
    may appear in the output."""
    phrases = _split(_vars(context).get("forbidden_phrases"))
    if not phrases:
        return _fail("instruction-miss", "vars.forbidden_phrases is empty — nothing to check")
    lowered = str(output).lower()
    hits = [p for p in phrases if p.lower() in lowered]
    if hits:
        return _fail("instruction-miss", f"forbidden phrase(s) present: {', '.join(hits)}")
    return _ok("no forbidden phrases")


def regex_required(output, context):
    """vars.regex_required must match somewhere in the output."""
    pattern = _vars(context).get("regex_required")
    if not pattern:
        return _fail("format", "vars.regex_required is empty — nothing to check")
    if _re.search(str(pattern), str(output), flags=_re.M):
        return _ok(f"pattern {pattern!r} matched")
    return _fail("format", f"pattern {pattern!r} not found")

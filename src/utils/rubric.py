"""Rubric files (judges/<suite>.md) are the single source of dimension names
and anchors. Format, one file per suite:

    # Rubric: <suite id>
    <free-text context for the judge>
    ## dimension: <name>
    What it measures: <one line>
    - 5: <anchor> ... - 1: <anchor>

build_judge_prompt() renders the quote -> reason -> score skeleton, JSON-only
reply, judge never asked for an overall. The output-length instruction caps
judge verbosity (unprompted mean was 3,676 tokens; the cap saves ~$4.6 per
full baseline with no information loss)."""

from __future__ import annotations

import re
from pathlib import Path

from src import config

_DIM_SPLIT = re.compile(r"^## dimension:\s*", re.M)
_ANCHOR = re.compile(r"^-\s*([1-5]):\s*(.+)$", re.M)


def parse(rubric_path: Path) -> dict:
    text = Path(rubric_path).read_text()
    sections = _DIM_SPLIT.split(text)
    context = re.sub(r"^# [^\n]*\n", "", sections[0]).strip()
    dimensions = []
    for section in sections[1:]:
        lines = section.split("\n")
        name = lines[0].strip()
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError(f'{rubric_path}: dimension name "{name}" must be snake_case')
        body = "\n".join(lines[1:])
        m = re.search(r"What it measures:\s*(.+)", body)
        measures = m.group(1) if m else ""
        anchors = {a: t.strip() for a, t in _ANCHOR.findall(body)}
        for level in "54321":
            if level not in anchors:
                raise ValueError(f'{rubric_path}: dimension "{name}" missing anchor for score {level}')
        dimensions.append({"name": name, "measures": measures, "anchors": anchors})
    if not dimensions:
        raise ValueError(f'{rubric_path}: no "## dimension:" sections found')
    return {"context": context, "dimensions": dimensions}


def for_suite(suite_id: str) -> dict:
    suite = config.suite_by_id(suite_id)
    if not suite or not suite.get("rubric"):
        raise ValueError(f'suite "{suite_id}" has no rubric: entry in eval_config.yaml')
    return {**parse(config.resolve(suite["rubric"])), "path": suite["rubric"]}


DEFAULT_TAXONOMY = ["truncation", "format", "missing-content", "hallucination",
                    "refusal", "instruction-miss", "tone", "other"]


def taxonomy() -> list[str]:
    """Controlled failure vocabulary (graded.taxonomy overrides the default).
    Categorized failures tell the user WHAT KIND of change to hypothesize."""
    tags = config.get().get("graded", {}).get("taxonomy")
    return [str(t) for t in tags] if tags else DEFAULT_TAXONOMY


def build_judge_prompt(rubric: dict, suite_id: str, bundle: str) -> str:
    dims = "\n\n".join(
        f"{d['name']}: {d['measures']}\n"
        + "\n".join(f"  {level}: {d['anchors'][level]}" for level in "54321")
        for d in rubric["dimensions"]
    )
    schema = (
        '{"dimensions": {'
        + ", ".join(f'"{d["name"]}": {{"evidence": "...", "reasoning": "...", "score": N}}' for d in rubric["dimensions"])
        + '}, "top_issue": "...", "top_issue_tag": "...", "suggested_prompt_fix": "..."}'
    )
    target = config.get()["agents"]["judge"].get("max_output_tokens_target")
    brevity = (
        f"\nKeep the entire reply under roughly {target} tokens: evidence is ONE short quote "
        "(max 2 lines) per dimension, reasoning one short paragraph.\n"
        if target else ""
    )
    context = f"\nContext:\n{rubric['context']}\n" if rubric["context"] else ""
    return f"""You are grading one output of the prompt "{suite_id}" against the rubric below.
The output is a bundle: a STDOUT section and zero or more FILE sections.
Grade ONLY what is in the bundle — no benefit of the doubt for things it "probably meant".
{context}
For EACH dimension, in this order:
1. QUOTE the exact lines from the bundle that are the evidence (or state "no evidence present").
2. REASON about the quote against the anchors — one short paragraph.
3. SCORE 1-5 using the anchors. Score dimensions independently; do not let one dimension bleed into another.
Do not reward length: judge against the anchors, not the word count.
{brevity}
{dims}

Then: name the single biggest issue, tag it as top_issue_tag with EXACTLY ONE of [{", ".join(taxonomy())}], and propose the smallest edit to the PROMPT (not the output) that would have prevented it.

Return ONLY this JSON, no markdown fences:
{schema}
The reply must be VALID JSON: escape every double quote inside evidence
strings as \\" and write newlines as \\n — one unescaped quote invalidates
the whole reply.

===== BUNDLE UNDER JUDGMENT =====
{bundle}"""

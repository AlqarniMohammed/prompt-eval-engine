"""Perturbation drafts: paraphrase existing probes with the CHEAP dataset
model. A prompt that passes only when the input is phrased one way is
brittle — real users rephrase.

Same contract as gen-cases: output is a DRAFT under datasets/generated/,
never auto-wired into a suite; a human reviews and merges keepers into the
graded spec. MOCK_DATASET=canned|malformed|malformed-once proves the path
offline for free.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import yaml

from src import config
from src.evaluators.llm_judge import extract_json_object, _strip_fences
from src.science.gen_cases import REALISM_CHARTER
from src.utils import model_client

_mock_calls = 0


def _mock_reply(mode: str, sources: list[str], variants: int) -> str:
    global _mock_calls
    _mock_calls += 1
    body = json.dumps({"perturbations": [
        {"source": s, "variants": [f"mock paraphrase {i + 1} of: {s}" for i in range(variants)]}
        for s in sources
    ]})
    if mode == "canned":
        return "```json\n" + body + "\n```"
    if mode == "malformed":
        return "Sure! Here are some rephrasings."
    if mode == "malformed-once":
        return '{"perturbations": broken' if _mock_calls == 1 else body
    raise ValueError(f'unknown MOCK_DATASET mode "{mode}"')


def _source_probes(suite_id: str, limit: int = 5) -> list[str]:
    probes: list[str] = []
    spec_path = config.specs_dir() / f"{suite_id}.yaml"
    if spec_path.exists():
        spec = yaml.safe_load(spec_path.read_text()) or {}
        probes += [v["probe"] for axis in (spec.get("axes") or {}).values()
                   for v in axis if isinstance(v, dict) and v.get("probe")]
    suite = config.suite_by_id(suite_id)
    if suite:
        for case in yaml.safe_load(config.resolve(suite["file"]).read_text()) or []:
            p = (case.get("vars") or {}).get("probe")
            if p:
                probes.append(str(p))
    deduped = list(dict.fromkeys(probes))
    return deduped[:limit]


def _build_prompt(sources: list[str], variants: int) -> str:
    listed = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sources))
    return (
        f"For EACH user input below, write {variants} paraphrases: SAME intent "
        "and topic, different ordinary-user wording.\n\n"
        f"{REALISM_CHARTER}\n\n"
        f"Inputs:\n{listed}\n\n"
        'Reply with ONLY a JSON object: {"perturbations": [{"source": "<the '
        'original input verbatim>", "variants": ["...", ...]}, ...]} — one entry '
        f"per input, exactly {variants} variants each, no fences, no prose."
    )


def _validate_reply(text: str, sources: list[str], variants: int) -> list[dict]:
    try:
        reply = json.loads(extract_json_object(_strip_fences(text)))
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e}): {str(text)[:120]}")
    entries = reply.get("perturbations") if isinstance(reply, dict) else None
    if not isinstance(entries, list) or len(entries) != len(sources):
        raise ValueError(f"expected {len(sources)} entries, got "
                         f"{len(entries) if isinstance(entries, list) else 'none'}")
    for e in entries:
        if not isinstance(e.get("source"), str) or not isinstance(e.get("variants"), list) \
                or len(e["variants"]) != variants \
                or not all(isinstance(v, str) and v for v in e["variants"]):
            raise ValueError(f"every entry needs a source + exactly {variants} string variants")
    return [{"source": e["source"], "variants": e["variants"]} for e in entries]


def generate_perturbations(suite_id: str, variants: int, out_dir=None) -> dict:
    """One dataset-model call → validated draft YAML of paraphrase variants.
    Re-asks exactly once on an invalid reply, then raises."""
    sources = _source_probes(suite_id)
    if not sources:
        raise ValueError(f"suite {suite_id} has no probes to perturb "
                         "(dataset cases and spec axes are empty)")
    mock_mode = os.environ.get("MOCK_DATASET")
    dataset_agent = config.get()["agents"].get("dataset")
    if dataset_agent is None:
        raise config.ConfigError("agents.dataset is not configured — perturb needs it")
    base_prompt = _build_prompt(sources, variants)

    entries = None
    last_error = None
    for attempt in range(2):
        prompt = base_prompt if attempt == 0 else (
            f"{base_prompt}\n\nYour previous reply was rejected: {last_error}. "
            "Reply again with ONLY the JSON object described above.")
        reply = _mock_reply(mock_mode, sources, variants) if mock_mode else \
            model_client.call(dataset_agent, "dataset", prompt)
        try:
            entries = _validate_reply(reply, sources, variants)
            break
        except ValueError as e:
            last_error = str(e)
    if entries is None:
        raise RuntimeError(f"perturbation generator invalid after one re-ask: {last_error}")

    out_dir = out_dir or config.graded_dir().parent / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{suite_id}-perturb-{stamp}.yaml"
    model_id = f"mock:{mock_mode}" if mock_mode else dataset_agent["model"]
    header = (
        f"# DRAFT — paraphrase variants generated by perturb ({model_id}) on {stamp}.\n"
        f"# NOT wired into any suite. Review each variant (same intent? still an\n"
        f"# ordinary user?), merge keepers into an axis of the graded spec, then:\n"
        f"#   prompt-eval matrix --suite {suite_id}\n"
    )
    out.write_text(header + yaml.safe_dump(
        {"suite": suite_id, "perturbations": entries},
        sort_keys=False, allow_unicode=True, width=88))
    return {"out": out, "perturbations": entries, "model": model_id}

"""Prompt functions referenced by every promptfoo config. Fully
declaration-driven: prompt dir, candidate dir, and the wrapper map come from
eval_config.yaml. The built prompt is
    <prompt file> + <wrapper boundary reminders, if declared> + <optional probe>."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from src import config

# Prompt sources beyond plain files: a module-level string constant, so real
# prompts that live in code don't have to be copied (copy = drift between
# what is tested and what ships). `.j2` templates are deliberately NOT
# supported: promptfoo already nunjucks-renders {{vars}} in the returned
# text, and a second template dialect would be a fairness hazard.
_PY_CONST = re.compile(r"^file://(.+\.py):([A-Za-z_]\w*)$")


def resolve_prompt_source(directory: Path, prompt_file: str) -> str:
    """Resolve a prompt source to its text.

        <name>.md / .txt        read from `directory`
        file://module.py:CONST  module-level string constant (repo-anchored
                                path; ignores `directory` — such a source has
                                no candidate arm and cannot be promoted)
    """
    m = _PY_CONST.match(str(prompt_file))
    if not m:
        return (Path(directory) / prompt_file).read_text()
    module_path = config.resolve(m.group(1))
    if not module_path.exists():
        raise FileNotFoundError(f"prompt source module missing: {m.group(1)}")
    spec = importlib.util.spec_from_file_location(f"_prompt_src_{module_path.stem}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, m.group(2), None)
    if not isinstance(value, str):
        raise ValueError(f"{prompt_file}: {m.group(2)} is not a module-level string constant")
    return value


def prompt_sha(directory: Path, prompt_file: str) -> str | None:
    """Fingerprint of a prompt source. Plain files keep the byte sha
    (identical to every existing record); file:// sources hash the RESOLVED
    text, so the state machine detects changes at the source."""
    from src import state
    if _PY_CONST.match(str(prompt_file)):
        try:
            return state.sha256_text(resolve_prompt_source(directory, prompt_file))
        except (FileNotFoundError, ValueError):
            return None
    return state.sha256_file(Path(directory) / prompt_file)


def strip_wrapper(text: str, wrappers: dict) -> str:
    if wrappers.get("strip_frontmatter", True):
        text = re.sub(r"^---[\s\S]*?---\s*", "", text)
    drop = [re.compile(p) for p in wrappers.get("strip_lines_matching", [])]
    return "\n".join(
        line for line in text.split("\n") if not any(p.search(line) for p in drop)
    ).strip()


def _build(prompt_dir: Path, variables: dict) -> str:
    prompt_file = variables.get("promptFile")
    if not prompt_file:
        raise ValueError("test is missing vars.promptFile")
    out = resolve_prompt_source(Path(prompt_dir), prompt_file).rstrip()

    wrappers = config.get()["prompts"].get("wrappers") or {}
    wrapper_file = (wrappers.get("map") or {}).get(prompt_file)
    if wrapper_file:
        raw = (config.resolve(wrappers["dir"]) / wrapper_file).read_text()
        text = strip_wrapper(raw, wrappers)
        if text:
            out += f"\n\n---\n# {wrappers['heading']}\n{text}"

    probe = variables.get("probe")
    transcript = variables.get("simulatedTranscript")
    if probe and transcript:
        raise ValueError("case declares both vars.probe and a simulated transcript "
                         "— the transcript's final user turn IS the probe")
    if transcript:
        out += "\n\n---\n" + str(transcript).strip()
    elif probe:
        out += "\n\n---\n" + str(probe).strip()
    return out


def current_prompt(context: dict) -> str:
    return _build(config.resolve(config.get()["prompts"]["production_dir"]), context.get("vars") or {})


def compare_prompt(context: dict) -> str:
    """Compare runs: the generator stamps vars.promptVariant on every test;
    one prompt function switches trees so each (cell, trial, variant) row
    keeps its own workspace and cache identity."""
    variant = (context.get("vars") or {}).get("promptVariant")
    if variant == "candidate":
        return candidate_prompt(context)
    if variant == "current":
        return current_prompt(context)
    raise ValueError(f"compare test is missing vars.promptVariant (got {variant!r})")


def candidate_prompt(context: dict) -> str:
    variables = context.get("vars") or {}
    directory = config.resolve(config.get()["prompts"].get("candidates_dir", "prompts/candidates"))
    if _PY_CONST.match(str(variables.get("promptFile"))):
        raise ValueError(
            f"{variables.get('promptFile')}: file:// prompt sources have no candidate "
            "arm — the compare/promote workflow needs plain prompt files")
    if not (directory / str(variables.get("promptFile"))).exists():
        raise FileNotFoundError(
            f"No candidate rewrite at {directory / str(variables.get('promptFile'))} — "
            "create it before running a compare."
        )
    return _build(directory, variables)

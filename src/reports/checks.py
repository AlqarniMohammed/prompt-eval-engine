"""Offline declaration-integrity checks (validate step 1): every declaration
in eval_config.yaml and every reference in the datasets resolves. Fully
generic — all project specifics come from the config."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

from src import config
from src.loaders.pf_prompts import current_prompt, resolve_prompt_source, strip_wrapper
from src.providers.mock_golden import golden_files
from src.utils import dataset_loader
from src.utils import rubric as rubric_lib
from src.utils.bundle import to_list

_PY_REF = re.compile(r"^file://(.+\.py):(\w+)$")
_TEMPLATE_VAR = re.compile(r"{{\s*(\w+)\s*}}")
_asserts_modules: dict[str, object] = {}

# The loud marker `prompt-eval init` stamps into every scaffolded value.
PLACEHOLDER = "EVAL-INIT-PLACEHOLDER"


def placeholder_files(suite: dict) -> list[str]:
    """Suite artifact files still carrying the init scaffold's placeholder —
    the suite works end to end, but each listed file is a proof not yet
    earned on real content."""
    candidates: list = []
    dataset = config.resolve(suite["file"])
    candidates.append(dataset)
    if suite.get("rubric"):
        candidates.append(config.resolve(suite["rubric"]))
    candidates.append(config.asserts_file())
    if dataset.exists():
        cases = yaml.safe_load(dataset.read_text()) or []
        if isinstance(cases, list):
            for case in cases:
                variables = (case.get("vars") or {}) if isinstance(case, dict) else {}
                if variables.get("promptFile"):
                    candidates.append(
                        config.resolve(config.get()["prompts"]["production_dir"])
                        / variables["promptFile"])
                if variables.get("golden"):
                    gdir = config.fixtures_dir() / "golden" / str(variables["golden"])
                    if gdir.exists():
                        candidates += [f for f in gdir.iterdir() if f.suffix == ".txt"]
    spec = config.specs_dir() / f'{suite["id"]}.yaml'
    candidates.append(spec)
    hits = []
    for path in dict.fromkeys(candidates):
        try:
            if path.exists() and PLACEHOLDER in path.read_text():
                hits.append(config.display_path(path))
        except (OSError, UnicodeDecodeError):
            continue
    return hits


def load_asserts_module(path_str: str):
    """Import the project's contract-checks module from the path a dataset's
    file:// assert reference names. Cached per path; raises on a missing file
    or a module that fails to import."""
    path = config.resolve(path_str)
    key = str(path)
    if key not in _asserts_modules:
        if not path.exists():
            raise FileNotFoundError(f"asserts file missing: {path_str}")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _asserts_modules[key] = module
    return _asserts_modules[key]


def run_checks() -> tuple[list[str], list[str]]:
    """Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    bad = errors.append
    warn = warnings.append
    cfg = config.get()
    suites = config.suites()
    prompts_dir = config.resolve(cfg["prompts"]["production_dir"])

    # Two-way suites <-> disk check.
    declared = {config.resolve(s["file"]) for s in suites}
    for dataset_dir in {config.resolve(s["file"]).parent for s in suites}:
        for f in dataset_dir.glob("*.yaml"):
            if f not in declared:
                bad(f"{config.display_path(f)} exists on disk but no suite declares it")

    cases = []
    for suite in suites:
        path = config.resolve(suite["file"])
        if not path.exists():
            bad(f'suite "{suite["id"]}" — dataset missing: {suite["file"]}')
            continue
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, list):
            bad(f'suite "{suite["id"]}" — {suite["file"]} is not a YAML list')
            continue
        cases += [{**c, "_suite": suite} for c in data]

    for case in cases:
        cid = f'{case["_suite"]["id"]}: {case.get("description") or "(no description)"}'
        if not case.get("description"):
            bad(f"{cid} — case has no description")
        variables = case.get("vars") or {}
        # promptfoo silently explodes YAML-array vars into a cartesian product.
        for key, value in variables.items():
            if isinstance(value, list):
                bad(f'{cid} — var "{key}" is a YAML array; write it as a comma-separated string')
        # Multi-turn simulation cases: structure + probe exclusivity, checked
        # free here so a live run never dies on them in the loader.
        if case.get("messages") is not None:
            for problem in dataset_loader.message_problems(case["messages"]):
                bad(f"{cid} — {problem}")
            if variables.get("probe"):
                bad(f"{cid} — messages: and vars.probe are mutually exclusive "
                    "(the final user turn IS the probe)")
        if not variables.get("promptFile"):
            bad(f"{cid} — no vars.promptFile")
        else:
            try:
                resolve_prompt_source(prompts_dir, variables["promptFile"])
            except (FileNotFoundError, ValueError, SyntaxError) as e:
                bad(f'{cid} — prompt source unresolvable: {e}')
        for fixture in to_list(variables.get("fixture")):
            if not (config.fixtures_dir() / fixture).exists():
                bad(f"{cid} — fixture missing: {fixture}")
        if not variables.get("golden"):
            bad(f"{cid} — no vars.golden")
        else:
            gdir = config.fixtures_dir() / "golden" / str(variables["golden"])
            for golden_set in ("pass", "fail"):
                if not golden_files(gdir, golden_set):
                    bad(f'{cid} — no golden {variables["golden"]}/{golden_set}*.txt')
        for a in case.get("assert") or []:
            m = _PY_REF.match(str(a.get("value") or ""))
            if not m:
                continue
            try:
                module = load_asserts_module(m.group(1))
            except Exception as e:
                bad(f"{cid} — {e}")
                continue
            if not hasattr(module, m.group(2)):
                bad(f'{cid} — assert function missing: {a["value"]}')

    # Template-variable fairness (item 25). nunjucks fills {{vars}} from the
    # case AFTER the python prompt function returns; a referenced var missing
    # from the case renders silently as empty text, and current/candidate
    # templates referencing different vars would hand one arm hidden inputs —
    # blinding is meaningless if the arms' inputs differ.
    candidates_dir = config.resolve(cfg["prompts"].get("candidates_dir", "prompts/candidates"))
    prompt_files = sorted({(c.get("vars") or {}).get("promptFile")
                           for c in cases if (c.get("vars") or {}).get("promptFile")})
    for prompt_file in prompt_files:
        try:
            text = resolve_prompt_source(prompts_dir, prompt_file)
        except (FileNotFoundError, ValueError, SyntaxError):
            continue  # already an error above
        if "{%" in text:
            warn(f"{prompt_file} — nunjucks logic blocks are not validated; "
                 "keep templates to plain {{var}} substitution")
        referenced = set(_TEMPLATE_VAR.findall(text))
        for case in cases:
            variables = case.get("vars") or {}
            if variables.get("promptFile") != prompt_file:
                continue
            missing = sorted(referenced - set(variables))
            if missing:
                cid = f'{case["_suite"]["id"]}: {case.get("description") or "(no description)"}'
                bad(f"{cid} — prompt {prompt_file} references "
                    + ", ".join("{{" + m + "}}" for m in missing)
                    + " but the case declares no such var (would render empty)")
        cand_path = candidates_dir / prompt_file
        if cand_path.exists():
            cand_referenced = set(_TEMPLATE_VAR.findall(cand_path.read_text()))
            if cand_referenced != referenced:
                diff = sorted(cand_referenced ^ referenced)
                bad(f"{prompt_file} — current and candidate templates reference "
                    f"different vars ({', '.join(diff)}); compared arms must "
                    "receive identical inputs")

    # Wrapper wiring (non-vacuous).
    wrappers = cfg["prompts"].get("wrappers") or {}
    for prompt_file, wrapper_file in (wrappers.get("map") or {}).items():
        wrapper_path = config.resolve(wrappers["dir"]) / wrapper_file
        if not wrapper_path.exists():
            bad(f"wrapper file missing: {wrappers['dir']}/{wrapper_file}")
            continue
        text = strip_wrapper(wrapper_path.read_text(), wrappers)
        built = current_prompt({"vars": {"promptFile": prompt_file, "probe": "x"}})
        if not text:
            warn(f"wrapper {wrapper_file} contributes no boundary text after stripping — {prompt_file} runs bare")
        elif wrappers["heading"] not in built:
            bad(f'built prompt for {prompt_file} missing wrapper heading "{wrappers["heading"]}"')

    # Rubrics parse with complete anchors.
    for suite in suites:
        if not suite.get("rubric"):
            continue
        try:
            r = rubric_lib.parse(config.resolve(suite["rubric"]))
            if not 3 <= len(r["dimensions"]) <= 5:
                warn(f'{suite["rubric"]} — {len(r["dimensions"])} dimensions (recommended 3-5)')
        except (ValueError, OSError) as e:
            bad(f'rubric {suite["rubric"]}: {e}')

    # Declarative canaries.
    for canary in (cfg.get("domain") or {}).get("canaries") or []:
        path = config.resolve(canary["file"])
        if not path.exists():
            bad(f"canary file missing: {canary['file']}")
            continue
        text = path.read_text()
        for token in canary.get("must_contain") or []:
            if token not in text:
                bad(f'{canary["file"]} — missing required token "{token}"')

    # Scaffold placeholders are warnings, never errors: the pipeline is
    # provable end to end on them, but each file listed is a proof not yet
    # earned on real content (and calibrate will refuse placeholder goldens).
    for suite in suites:
        remaining = placeholder_files(suite)
        if remaining:
            warn(f'suite "{suite["id"]}" is scaffolded — {PLACEHOLDER} remains in: '
                 + ", ".join(remaining))

    # Graded matrices get the same array-var and messages lints.
    graded_dir = config.graded_dir()
    if graded_dir.exists():
        for f in graded_dir.glob("*.yaml"):
            for c in yaml.safe_load(f.read_text()) or []:
                for key, value in (c.get("vars") or {}).items():
                    if isinstance(value, list):
                        bad(f'graded/{f.name}: var "{key}" is a YAML array')
                if c.get("messages") is not None:
                    for problem in dataset_loader.message_problems(c["messages"]):
                        bad(f"graded/{f.name}: {problem}")
                    if (c.get("vars") or {}).get("probe"):
                        bad(f"graded/{f.name}: messages: and vars.probe are mutually exclusive")

    return errors, warnings

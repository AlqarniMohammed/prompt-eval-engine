"""Per-item orchestration for `prompt-eval init`: emit the scaffold (or
relocate a bundle), edit the config, regenerate the graded matrix, teach
what each file proves, and ledger the whole adoption.

Every target directory derives from the config declarations (nothing is
hardcoded to this repo's layout): the project layer root is the parent of
the graded datasets dir, exactly as gen-cases already derives its output
location.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src import config
from src.scaffold import config_edit, emit, templates
from src.scaffold.detect import Detection
from src.utils import rubric as rubric_lib


class InitError(Exception):
    pass


# ------------------------------------------------------- path derivations —
def project_root() -> Path:
    return config.graded_dir().parent.parent


def datasets_dir() -> Path:
    return config.graded_dir().parent


def judges_dir() -> Path:
    return project_root() / "judges"


def input_dir() -> Path:
    return project_root() / "input"


def asserts_target() -> Path:
    declared = (config.get().get("project") or {}).get("asserts_file")
    if declared:
        return config.resolve(declared)
    return project_root() / "project" / "asserts.py"


def wrappers_dir() -> Path:
    w = config.get()["prompts"].get("wrappers") or {}
    if w.get("dir"):
        return config.resolve(w["dir"])
    return config.resolve(config.get()["prompts"]["production_dir"]).parent / "wrappers"


def _production_dir() -> Path:
    return config.resolve(config.get()["prompts"]["production_dir"])


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------- teaching —
STAGE_LINES = {
    "prompt": ("all stages", "the prompt under test — every run builds on this text"),
    "dataset": ("validate → baseline", "declares the test cases; validate proves every "
                "reference resolves before a cent is spent"),
    "asserts": ("validate + every run", "deterministic contracts — free checks that run on "
                "every tier and catch what a judge can miss"),
    "rubric": ("calibrate → graded", "the anchored 1-5 dimensions the judge scores against; "
               "calibration proves the judge lands your goldens in band"),
    "golden-pass": ("validate + calibrate", "a known-GOOD output; validate proves it passes "
                    "every contract, calibration proves the judge scores it high"),
    "golden-fail": ("validate + calibrate", "a known-BAD output; validate proves your contracts "
                    "actually catch it, calibration proves the judge scores it low"),
    "spec": ("matrix → graded", "axes that compose into the graded matrix — coverage by "
             "design instead of hand-picked cases"),
    "matrix": ("graded", "the generated matrix cells the graded tier scores (k trials each)"),
    "workspace": ("live runs", "each trial gets a fresh copy of this dir; files the agent "
                  "creates or edits are judged as part of the bundle"),
    "wrapper": ("all stages", "production fidelity — the prompt is tested with the same "
                "boundary text your product appends"),
    "config": ("all stages", "the declaration that wires the suite into the pipeline"),
}


# --------------------------------------------------------- scaffold types —
def _scaffold(det: Detection, dry: bool,
              rubric_kind: str | None = None) -> tuple[list[emit.Action], dict]:
    """Types prompt/wrapped/agent: move the prompt, emit the full scaffold.
    Returns (actions, context) where context carries names for config ops."""
    sid = det.suite_id
    actions: list[emit.Action] = []
    prompt_name = f"{sid}.md"
    actions.append(emit.move(det.prompt, _production_dir() / prompt_name,
                             *STAGE_LINES["prompt"], dry))

    is_agent = det.kind == "agent"
    fixture_name = None
    allowed_tools = det.manifest.get("allowedTools") if det.manifest else None
    if is_agent and not allowed_tools:
        allowed_tools = "Read,Write"
    if det.extras or is_agent:
        fixture_name = (det.manifest or {}).get("fixture") or f"{sid}-workspace"
        workspace = config.fixtures_dir() / fixture_name
        for extra in det.extras:
            actions.append(emit.move_tree(extra, workspace, *STAGE_LINES["workspace"], dry))
        if not det.extras:
            actions.append(emit.write(workspace / "README.md",
                                      templates.workspace_readme(sid),
                                      *STAGE_LINES["workspace"], dry))

    asserts_path = asserts_target()
    actions.append(emit.append_function(asserts_path, templates.ASSERTS_MODULE,
                                        templates.ASSERTS_FUNCTION,
                                        *STAGE_LINES["asserts"], dry))
    asserts_ref = f"file://{emit.rel(asserts_path)}:{templates.ASSERTS_FUNCTION}"

    dataset_path = datasets_dir() / f"{sid}.yaml"
    actions.append(emit.write(
        dataset_path,
        templates.dataset_yaml(sid, prompt_name, asserts_ref,
                               fixture=fixture_name, allowed_tools=allowed_tools),
        *STAGE_LINES["dataset"], dry))

    rubric_path = judges_dir() / f"{sid}.md"
    actions.append(emit.write(rubric_path, templates.rubric_md(sid, rubric_kind),
                              *STAGE_LINES["rubric"], dry))

    gdir = config.fixtures_dir() / "golden" / sid
    actions.append(emit.write(gdir / "pass.txt",
                              templates.golden_pass(sid, with_file_section=is_agent),
                              *STAGE_LINES["golden-pass"], dry))
    actions.append(emit.write(gdir / "fail.txt", templates.golden_fail(sid),
                              *STAGE_LINES["golden-fail"], dry))

    spec_path = config.specs_dir() / f"{sid}.yaml"
    actions.append(emit.write(
        spec_path,
        templates.graded_spec_yaml(sid, prompt_name, asserts_ref,
                                   fixture=fixture_name, allowed_tools=allowed_tools),
        *STAGE_LINES["spec"], dry))

    context = {
        "prompt_name": prompt_name,
        "dataset_path": dataset_path,
        "rubric_path": rubric_path,
        "spec_path": spec_path,
        "asserts_path": asserts_path,
    }
    if det.kind == "wrapped":
        wrapper_name = det.wrapper.name
        actions.append(emit.move(det.wrapper, wrappers_dir() / wrapper_name,
                                 *STAGE_LINES["wrapper"], dry))
        context["wrapper_name"] = wrapper_name
    return actions, context


# ------------------------------------------------------------ bundle type —
_PY_REF = re.compile(r"file://([^:\s\"']+\.py):(\w+)")


def _bundle_parts(det: Detection) -> dict:
    from src.scaffold.detect import is_rubric, looks_like_cases, looks_like_spec
    entries = sorted(p for p in det.source.iterdir()
                     if p.name != "input.yaml" and not p.name.startswith("."))
    parts = {"prompts": [], "rubrics": [], "datasets": [], "specs": [],
             "pys": [], "golden_dirs": [], "loose_goldens": [], "fixtures": []}
    for p in entries:
        if p.is_dir():
            if any(f.suffix == ".txt" and f.name.startswith(("pass", "fail", "mid"))
                   for f in p.iterdir() if f.is_file()):
                parts["golden_dirs"].append(p)
            elif p.name.lower() in ("golden", "goldens"):
                parts["golden_dirs"] += [d for d in sorted(p.iterdir()) if d.is_dir()]
            else:
                parts["fixtures"].append(p)
        elif p.suffix == ".md":
            (parts["rubrics"] if is_rubric(p) else parts["prompts"]).append(p)
        elif p.suffix in (".yaml", ".yml"):
            if looks_like_spec(p):
                parts["specs"].append(p)
            elif looks_like_cases(p):
                parts["datasets"].append(p)
            else:
                parts["fixtures"].append(p)
        elif p.suffix == ".py":
            parts["pys"].append(p)
        elif p.suffix == ".txt" and p.name.startswith(("pass", "fail", "mid")):
            parts["loose_goldens"].append(p)
        else:
            parts["fixtures"].append(p)
    return parts


def _bundle_validate(det: Detection, parts: dict) -> tuple[list[str], list[str], dict]:
    """ALL errors collected before anything moves. Returns (errors, warnings,
    info) where info carries parsed cases/golden names for the move step."""
    errors, warnings = [], []
    info: dict = {}
    name = det.source.name

    if len(parts["prompts"]) != 1:
        errors.append(f"{name}: need exactly one prompt .md (found {len(parts['prompts'])})")
    if len(parts["datasets"]) != 1:
        errors.append(f"{name}: need exactly one cases yaml (found {len(parts['datasets'])})")
    if len(parts["rubrics"]) > 1:
        errors.append(f"{name}: more than one rubric .md")
    if len(parts["specs"]) > 1:
        errors.append(f"{name}: more than one graded spec yaml")

    prompt = parts["prompts"][0] if len(parts["prompts"]) == 1 else None
    cases = []
    if len(parts["datasets"]) == 1:
        try:
            cases = yaml.safe_load(parts["datasets"][0].read_text()) or []
        except yaml.YAMLError as e:
            errors.append(f"{name}: {parts['datasets'][0].name} does not parse: {e}")
    golden_names: list[str] = []
    for i, case in enumerate(cases):
        cid = f"{name}: case {i + 1}"
        if not isinstance(case, dict):
            errors.append(f"{cid} is not a mapping")
            continue
        if not case.get("description"):
            errors.append(f"{cid} has no description")
        variables = case.get("vars") or {}
        pf = variables.get("promptFile")
        if prompt and pf != prompt.name:
            errors.append(f'{cid}: vars.promptFile {pf!r} != bundled prompt "{prompt.name}"')
        g = variables.get("golden")
        if g and g not in golden_names:
            golden_names.append(str(g))
        for key, value in variables.items():
            if isinstance(value, list):
                errors.append(f'{cid}: var "{key}" is a YAML array — use a comma-separated string')

    if parts["rubrics"]:
        try:
            rubric_lib.parse(parts["rubrics"][0])
        except (ValueError, OSError) as e:
            errors.append(f"{name}: rubric {parts['rubrics'][0].name}: {e}")

    if parts["specs"]:
        spec = yaml.safe_load(parts["specs"][0].read_text()) or {}
        if spec.get("suite") != det.suite_id:
            errors.append(f'{name}: spec declares suite "{spec.get("suite")}" but this bundle '
                          f'registers as "{det.suite_id}" — align them (or pass --suite)')
        if not spec.get("axes"):
            errors.append(f"{name}: spec has no axes")

    # file:// refs must resolve post-move: either against the repo already,
    # or against a bundled .py (rewritten to its new location).
    py_names = {p.name: p for p in parts["pys"]}
    ref_texts = [parts["datasets"][0].read_text()] if len(parts["datasets"]) == 1 else []
    ref_texts += [s.read_text() for s in parts["specs"]]
    rewrites: dict[str, str] = {}
    for text in ref_texts:
        for path_str, fn in _PY_REF.findall(text):
            base = Path(path_str).name
            if base in py_names:
                if f"def {fn}(" not in py_names[base].read_text():
                    errors.append(f"{name}: {base} does not define {fn}()")
                rewrites[path_str] = emit.rel(project_root() / "project" / base)
            elif config.resolve(path_str).exists():
                mod_text = config.resolve(path_str).read_text()
                if f"def {fn}(" not in mod_text:
                    errors.append(f"{name}: {path_str} exists but does not define {fn}()")
            else:
                errors.append(f"{name}: assert ref file://{path_str}:{fn} resolves to nothing "
                              "(not in the bundle, not in the repo)")

    golden_dir_names = {d.name for d in parts["golden_dirs"]}
    missing_goldens = []
    for g in golden_names:
        if g not in golden_dir_names and not (
                len(golden_names) == 1 and parts["loose_goldens"]):
            if not (config.fixtures_dir() / "golden" / g).exists():
                missing_goldens.append(g)
                warnings.append(f"{name}: no golden files for \"{g}\" — placeholders will be "
                                "emitted (replace before calibration)")
    info.update(prompt=prompt, cases=cases, golden_names=golden_names,
                rewrites=rewrites, missing_goldens=missing_goldens)
    return errors, warnings, info


def _bundle_moves(det: Detection, parts: dict, info: dict, dry: bool) -> list[emit.Action]:
    sid = det.suite_id
    actions = []
    actions.append(emit.move(info["prompt"], _production_dir() / info["prompt"].name,
                             *STAGE_LINES["prompt"], dry))

    def _move_rewritten(src: Path, dest: Path, stage_key: str):
        text = src.read_text()
        for old, new in info["rewrites"].items():
            text = text.replace(f"file://{old}", f"file://{new}")
        action = emit.write(dest, text, *STAGE_LINES[stage_key], dry)
        if action.status in ("created", "planned") and not dry:
            src.unlink()
        action.kind, action.source = "move", src
        actions.append(action)

    _move_rewritten(parts["datasets"][0], datasets_dir() / f"{sid}.yaml", "dataset")
    if parts["rubrics"]:
        _move_rewritten(parts["rubrics"][0], judges_dir() / f"{sid}.md", "rubric")
    if parts["specs"]:
        _move_rewritten(parts["specs"][0], config.specs_dir() / f"{sid}.yaml", "spec")
    for py in parts["pys"]:
        actions.append(emit.move(py, project_root() / "project" / py.name,
                                 *STAGE_LINES["asserts"], dry))
    for gdir in parts["golden_dirs"]:
        for f in sorted(gdir.iterdir()):
            if f.is_file():
                actions.append(emit.move(
                    f, config.fixtures_dir() / "golden" / gdir.name / f.name,
                    *STAGE_LINES["golden-pass" if f.name.startswith("pass") else "golden-fail"],
                    dry))
    if parts["loose_goldens"] and info["golden_names"]:
        g = info["golden_names"][0]
        for f in parts["loose_goldens"]:
            actions.append(emit.move(
                f, config.fixtures_dir() / "golden" / g / f.name,
                *STAGE_LINES["golden-pass" if f.name.startswith("pass") else "golden-fail"],
                dry))
    for g in info["missing_goldens"]:
        gdir = config.fixtures_dir() / "golden" / g
        actions.append(emit.write(gdir / "pass.txt", templates.golden_pass(sid),
                                  *STAGE_LINES["golden-pass"], dry))
        actions.append(emit.write(gdir / "fail.txt", templates.golden_fail(sid),
                                  *STAGE_LINES["golden-fail"], dry))
    for fx in parts["fixtures"]:
        actions.append(emit.move_tree(fx, config.fixtures_dir(), *STAGE_LINES["workspace"], dry))
    return actions


# ------------------------------------------------------------ the process —
def process(det: Detection, dry_run: bool = False, print_config: bool = False,
            log=print, rubric_kind: str | None = None) -> dict:
    sid = det.suite_id
    declared = config.suite_by_id(sid)

    if det.kind == "bundle" and rubric_kind:
        raise InitError("--rubric only applies to scaffolded suites — a bundle "
                        "brings its own rubric")
    if det.kind == "bundle":
        parts = _bundle_parts(det)
        errors, warnings, info = _bundle_validate(det, parts)
        if errors:
            raise InitError("bundle pre-flight failed (nothing was moved):\n  "
                            + "\n  ".join(errors))
        for w in warnings:
            log(f" WARN {w}")
        try:
            actions = _bundle_moves(det, parts, info, dry_run)
        except emit.EmitError as e:
            raise InitError(str(e))
        dataset_path = datasets_dir() / f"{sid}.yaml"
        rubric_path = (judges_dir() / f"{sid}.md") if parts["rubrics"] else None
        spec_path = (config.specs_dir() / f"{sid}.yaml") if parts["specs"] else None
        context = {"dataset_path": dataset_path, "rubric_path": rubric_path,
                   "spec_path": spec_path,
                   "asserts_path": (project_root() / "project" / parts["pys"][0].name)
                   if parts["pys"] else None}
    else:
        if declared:
            declared_file = config.resolve(declared["file"])
            if declared_file != (datasets_dir() / f"{sid}.yaml").resolve():
                raise InitError(
                    f'suite "{sid}" is already declared with a different dataset '
                    f"({declared['file']}) — pick another id with --suite")
        try:
            actions, context = _scaffold(det, dry_run, rubric_kind)
        except emit.EmitError as e:
            raise InitError(str(e))

    # ---- config edits ----
    ops = []
    if not declared:
        rubric_rel = emit.rel(context["rubric_path"]) if context.get("rubric_path") else None
        ops.append(config_edit.suite_entry_op(sid, emit.rel(context["dataset_path"]), rubric_rel))
    if context.get("asserts_path") and not (config.get().get("project") or {}).get("asserts_file"):
        ops.append(config_edit.asserts_file_op(emit.rel(context["asserts_path"])))
    if context.get("wrapper_name"):
        wrappers = config.get()["prompts"].get("wrappers") or {}
        if (wrappers.get("map") or {}).get(context["prompt_name"]) != context["wrapper_name"]:
            ops.append(config_edit.wrapper_map_op(
                context["prompt_name"], context["wrapper_name"],
                emit.rel(wrappers_dir()), wrappers or None))

    edit = config_edit.EditResult(applied=True)
    if ops and (print_config or dry_run):
        log("\n-- config edit " + ("(--print-config: paste this yourself)" if print_config
                                   else "(dry run, not applied)") + " --")
        for op in ops:
            log(op.snippet)
        edit = config_edit.EditResult(applied=False, descriptions=[o.description for o in ops])
    elif ops:
        edit = config_edit.apply(config.config_path(), ops)
        if not edit.applied:
            raise InitError(
                f"config edit failed: {edit.error}\nPaste this into "
                f"{emit.rel(config.config_path())} yourself:\n{edit.snippet}")

    # ---- graded matrix ----
    matrix = None
    if context.get("spec_path") and not dry_run and edit.applied and config.suite_by_id(sid):
        from src.science.gen_matrix import generate
        matrix = generate(Path(context["spec_path"]))
        actions.append(emit.Action("write", matrix["out"], "created",
                                   stage=STAGE_LINES["matrix"][0],
                                   proves=STAGE_LINES["matrix"][1]))

    summary = {
        "suite": sid,
        "kind": det.kind,
        "source": str(det.source),
        "dryRun": dry_run,
        "actions": [{"kind": a.kind, "dest": emit.rel(a.dest), "status": a.status,
                     "source": (str(a.source) if a.source else None),
                     "stage": a.stage, "proves": a.proves} for a in actions],
        "configEdits": edit.descriptions,
        "configApplied": edit.applied,
        "matrixCells": matrix["cells"] if matrix else None,
    }
    _teach(summary, log)
    if not dry_run:
        _ledger(summary)
        _cleanup_source(det.source)
    return summary


def _cleanup_source(source: Path):
    """Remove the emptied drop folder (and leftover input.yaml) once every
    payload file has moved out — the drop box empties as items are adopted."""
    source = Path(source)
    if not source.is_dir():
        return
    leftover = source / "input.yaml"
    if leftover.exists():
        leftover.unlink()
    for d in sorted(source.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    if not any(source.iterdir()):
        source.rmdir()


def _teach(summary: dict, log):
    sid = summary["suite"]
    created = [a for a in summary["actions"] if a["status"] in ("created", "planned")]
    kept = [a for a in summary["actions"] if a["status"].startswith("exists")]
    header = "planned (dry run)" if summary["dryRun"] else "registered"
    log("─" * 72)
    log(f"init      suite {sid} ({summary['kind']}) {header}")
    log("─" * 72)
    if created:
        log("\nFROM → TO")
        for a in created:
            src = Path(a["source"]).name if a["source"] else "(scaffolded)"
            log(f"  {src:<28} → {a['dest']}")
    if kept:
        log(f"\n  {len(kept)} file(s) already in place were kept untouched")
    if summary["configEdits"]:
        state = "" if summary["configApplied"] else "  (NOT applied — see above)"
        log(f"\nCONFIG    {' · '.join(summary['configEdits'])}{state}")
    if summary["matrixCells"]:
        log(f"MATRIX    {summary['matrixCells']} graded cells generated")

    log("\nWHAT EACH FILE PROVES")
    seen = set()
    for a in summary["actions"]:
        if a["proves"] and a["dest"] not in seen:
            seen.add(a["dest"])
            log(f"  {a['dest']}\n      [{a['stage']}] {a['proves']}")

    if summary["kind"] == "bundle":
        # A bundle ships real content — there are no placeholders to replace.
        log(f"""
NEXT STEPS (step 1 costs $0; nothing paid can run before step 2)
  1. $0  uv run prompt-eval validate     # should be green right now
  2. ~$  uv run prompt-eval calibrate --suite {sid}   # first paid step
  3. ~$  preflight, then graded --suite {sid} --filter-first-n 2, then the full graded baseline""")
    else:
        log(f"""
NEXT STEPS (steps 1-5 cost $0; nothing paid can run before step 6)
  1. $0  replace the placeholder probes in the dataset with real user messages
  2. $0  replace the golden pass/fail bundles with real known-good/known-bad outputs
  3. $0  rewrite the rubric anchors for your quality bar
  4. $0  replace starter_contract with real deterministic checks
  5. $0  uv run prompt-eval validate     # green already; stays green as you edit
  6. ~$  uv run prompt-eval calibrate --suite {sid}   # first paid step; refuses placeholder goldens
  7. ~$  preflight, then graded --suite {sid} --filter-first-n 2, then the full graded baseline""")
    others = [s for s in (config.load(force_reload=True).get("suites") or [])
              if s.get("id") != sid]
    if summary["configApplied"] and others:
        log("NOTE  this edited config/eval_config.yaml, so other suites' `validated` stage\n"
            "      was staleness-wiped — `prompt-eval graft` restores them for free.")


def _ledger(summary: dict):
    out = config.history_dir() / f"init-{_stamp()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**summary, "at": datetime.now(timezone.utc).isoformat()},
                              indent=2))

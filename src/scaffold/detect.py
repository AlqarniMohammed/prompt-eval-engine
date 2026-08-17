"""Structural detection for the input/ drop box.

Decision tree (cheap, structural — at most ONE question ever reaches the
human, and only for the single genuinely ambiguous case: bare prompt vs
agent prompt):

    single .md file          -> prompt, unless tool-use markers suggest agent
                                (then ambiguous: default agent, confirm)
    folder with input.yaml   -> obey the manifest
    folder with a cases yaml,
      a rubric (## dimension:),
      or golden pass*/fail*  -> bundle (register, don't scaffold)
    folder with two .md, one
      frontmattered or named
      *wrapper*/*command*    -> prompt + wrapper
    folder with one .md plus
      other files            -> prompt whose extras become the fixture
                                workspace (raises the agent default)

--as / input.yaml `type` / --yes are the non-interactive escape hatches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = ("prompt", "wrapped", "agent", "bundle")


class DetectError(Exception):
    pass


@dataclass
class Detection:
    source: Path
    suite_id: str
    kind: str | None            # None = ambiguous, needs confirmation
    default_kind: str           # what --yes accepts
    prompt: Path | None = None
    wrapper: Path | None = None
    extras: list[Path] = field(default_factory=list)   # files/dirs -> fixture workspace
    manifest: dict = field(default_factory=dict)       # input.yaml contents
    reasons: list[str] = field(default_factory=list)   # why this classification


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not s:
        raise DetectError(f"cannot derive a suite id from {name!r}")
    return s


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.S)
_FM_TOOLS = re.compile(r"^(allowed[-_]tools|allowedtools|tools)\s*:", re.I | re.M)
# A comma-joined tool list ("Read,Write" / "Read, Write, Bash") in prose or
# frontmatter — single capitalized tool words alone are too false-positive-y.
_TOOL_LIST = re.compile(
    r"\b(Read|Write|Edit|Bash|Glob|Grep|WebFetch|WebSearch)\s*,\s*"
    r"(Read|Write|Edit|Bash|Glob|Grep|WebFetch|WebSearch)\b")
_WORKSPACE = re.compile(r"\bworkspace\b", re.I)


def agent_markers(text: str) -> list[str]:
    found = []
    fm = _FRONTMATTER.match(text)
    if fm and _FM_TOOLS.search(fm.group(1)):
        found.append("frontmatter declares tools")
    if _TOOL_LIST.search(text):
        found.append('tool list ("Read,Write"-style)')
    if _WORKSPACE.search(text):
        found.append('mentions "workspace"')
    return found


def is_rubric(path: Path) -> bool:
    try:
        return "## dimension:" in path.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def _yaml_of(path: Path):
    try:
        return yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None


def looks_like_cases(path: Path) -> bool:
    data = _yaml_of(path)
    return (isinstance(data, list) and bool(data)
            and all(isinstance(c, dict) for c in data)
            and any("vars" in c or "assert" in c for c in data))


def looks_like_spec(path: Path) -> bool:
    data = _yaml_of(path)
    return isinstance(data, dict) and "axes" in data


def has_goldens(path: Path) -> bool:
    """pass*/fail* .txt files anywhere one level down (a goldens dir) or loose."""
    def golden_names(d: Path) -> bool:
        return any(f.suffix == ".txt" and f.name.startswith(("pass", "fail"))
                   for f in d.iterdir() if f.is_file())
    if golden_names(path):
        return True
    return any(golden_names(d) for d in path.iterdir() if d.is_dir())


def _is_wrapperish(path: Path) -> bool:
    name = path.stem.lower()
    if "wrapper" in name or "command" in name:
        return True
    return bool(_FRONTMATTER.match(path.read_text()))


def detect(path: Path, as_kind: str | None = None, suite: str | None = None) -> Detection:
    path = Path(path)
    if as_kind and as_kind not in KINDS:
        raise DetectError(f"--as must be one of {', '.join(KINDS)}")

    if path.is_file():
        if path.suffix != ".md":
            raise DetectError(
                f"{path.name}: a single dropped file must be a .md prompt — "
                "drop folders for anything richer (see input/README.md)")
        sid = suite or slug(path.stem)
        markers = agent_markers(path.read_text())
        if as_kind:
            if as_kind in ("wrapped", "bundle"):
                raise DetectError(f"{path.name}: --as {as_kind} needs a folder, not a single file")
            return Detection(path, sid, as_kind, as_kind, prompt=path, reasons=markers)
        if markers:
            return Detection(path, sid, None, "agent", prompt=path, reasons=markers)
        return Detection(path, sid, "prompt", "prompt", prompt=path,
                         reasons=["single .md, no tool-use markers"])

    if not path.is_dir():
        raise DetectError(f"{path} does not exist")

    manifest = {}
    manifest_path = path / "input.yaml"
    if manifest_path.exists():
        manifest = _yaml_of(manifest_path) or {}
        if not isinstance(manifest, dict):
            raise DetectError(f"{path.name}/input.yaml is not a YAML mapping")

    entries = sorted(p for p in path.iterdir()
                     if p.name != "input.yaml" and not p.name.startswith(".")
                     and p.name != "__pycache__" and p.suffix != ".pyc")
    if not entries:
        raise DetectError(f"{path.name}/ is empty")
    mds = [p for p in entries if p.is_file() and p.suffix == ".md"]
    rubrics = [m for m in mds if is_rubric(m)]
    prompt_mds = [m for m in mds if m not in rubrics]
    yamls = [p for p in entries if p.is_file() and p.suffix in (".yaml", ".yml")]
    sid = manifest.get("suite") or suite or slug(path.name)

    kind = as_kind or manifest.get("type")
    if kind and kind not in KINDS:
        raise DetectError(f'{path.name}: type "{kind}" must be one of {", ".join(KINDS)}')

    is_bundle = bool(rubrics or any(looks_like_cases(y) for y in yamls) or has_goldens(path))
    if not kind and is_bundle:
        kind = "bundle"
        reasons = [r for r, hit in (
            ("rubric with ## dimension:", bool(rubrics)),
            ("cases yaml", any(looks_like_cases(y) for y in yamls)),
            ("golden pass*/fail* files", has_goldens(path)),
        ) if hit]
    else:
        reasons = []

    if kind == "bundle":
        return Detection(path, sid, "bundle", "bundle", manifest=manifest,
                         reasons=reasons or ["declared bundle"])

    if not prompt_mds:
        raise DetectError(f"{path.name}/: no prompt .md found")

    # prompt + wrapper: exactly two non-rubric .md, one wrapper-ish
    wrapper_name = manifest.get("wrapper")
    if wrapper_name:
        wrapper = path / wrapper_name
        if not wrapper.exists():
            raise DetectError(f'{path.name}/: input.yaml names wrapper "{wrapper_name}" but it is missing')
        prompts = [m for m in prompt_mds if m != wrapper]
        if len(prompts) != 1:
            raise DetectError(f"{path.name}/: expected exactly one prompt .md besides the wrapper")
        return Detection(path, sid, kind or "wrapped", kind or "wrapped",
                         prompt=prompts[0], wrapper=wrapper, manifest=manifest,
                         reasons=["input.yaml declares the wrapper"])
    folder_named = [m for m in prompt_mds if slug(m.stem) == slug(path.name)]
    if not kind and len(prompt_mds) >= 2:
        # A wrapper is either name-flagged (*wrapper*/*command*) or, between
        # exactly two files, the frontmattered one — unless that file is named
        # like the folder (then IT is the prompt and the folder is an agent
        # drop with support files, not a wrapped pair).
        name_flagged = [m for m in prompt_mds
                        if "wrapper" in m.stem.lower() or "command" in m.stem.lower()]
        wrapper = None
        if len(name_flagged) == 1 and len(prompt_mds) == 2:
            wrapper = name_flagged[0]
            why = f"{wrapper.name} is wrapper-named"
        elif len(prompt_mds) == 2 and not name_flagged:
            frontmattered = [m for m in prompt_mds if _is_wrapperish(m)]
            if len(frontmattered) == 1 and frontmattered[0] not in folder_named:
                wrapper = frontmattered[0]
                why = f"{wrapper.name} is frontmattered and not named like the folder"
        if wrapper is not None:
            prompt = next(m for m in prompt_mds if m != wrapper)
            return Detection(path, sid, "wrapped", "wrapped", prompt=prompt,
                             wrapper=wrapper, manifest=manifest, reasons=[why])

    if len(prompt_mds) == 1:
        prompt = prompt_mds[0]
    elif len(folder_named) == 1:
        prompt = folder_named[0]  # the folder-named .md is the prompt; the rest are support files
    else:
        raise DetectError(
            f"{path.name}/: {len(prompt_mds)} candidate prompt .md files — "
            "name one after the folder, or add input.yaml (wrapper:/type:) to disambiguate")
    extras = [p for p in entries if p != prompt]
    markers = agent_markers(prompt.read_text())

    if kind:  # --as / input.yaml type
        return Detection(path, sid, kind, kind, prompt=prompt, extras=extras,
                         manifest=manifest, reasons=markers)
    if extras:
        reasons = markers + [f"{len(extras)} support file(s) suggest a workspace"]
        return Detection(path, sid, None, "agent", prompt=prompt, extras=extras,
                         manifest=manifest, reasons=reasons)
    if markers:
        return Detection(path, sid, None, "agent", prompt=prompt,
                         manifest=manifest, reasons=markers)
    return Detection(path, sid, "prompt", "prompt", prompt=prompt,
                     manifest=manifest, reasons=["one .md, no tool-use markers"])

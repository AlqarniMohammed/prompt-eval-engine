"""Anchored textual edits to eval_config.yaml for `prompt-eval init`.

Three narrow operations only (insert a suite entry — including into the
empty `[]` form; set project.asserts_file; insert/extend the prompts.wrappers
block), applied as TEXT edits so every comment in the declaration file
survives. Safety net, in order:

  1. a `.bak` copy of the original is written next to the config,
  2. the edited text must parse to exactly the original data plus the
     declared semantic delta (yaml.safe_load before + delta == after),
  3. config._validate() must accept the result,
  4. any failure rolls the file back and returns the snippet to paste by
     hand — the edit never half-lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from src import config


class ConfigEditError(Exception):
    """Carries the paste-this-instead snippet in .snippet."""

    def __init__(self, msg: str, snippet: str = ""):
        super().__init__(msg)
        self.snippet = snippet


@dataclass
class Op:
    description: str
    apply_text: Callable[[str], str]
    apply_data: Callable[[dict], None]     # mutates the expected parsed dict
    snippet: str = ""                      # manual-paste fallback


def _block_span(lines: list[str], key: str) -> tuple[int, int]:
    """(header_index, end_index_exclusive) of a top-level `key:` block. The
    block runs while lines are blank, comments, or indented."""
    start = next((i for i, ln in enumerate(lines)
                  if ln == f"{key}:" or ln.startswith(f"{key}:")), None)
    if start is None:
        raise ConfigEditError(f'config has no top-level "{key}:" block')
    j = start + 1
    end = j
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "" or ln.startswith((" ", "\t", "#")):
            if ln.startswith((" ", "\t")):
                end = j + 1  # last indented (content) line so far
            j += 1
            continue
        break
    return start, max(end, start + 1)


def suite_entry_op(suite_id: str, file_rel: str, rubric_rel: str | None) -> Op:
    entry_lines = [f"  - id: {suite_id}", f"    file: {file_rel}"]
    if rubric_rel:
        entry_lines.append(f"    rubric: {rubric_rel}")
    entry_text = "\n".join(entry_lines)

    def apply_text(text: str) -> str:
        lines = text.split("\n")
        start, end = _block_span(lines, "suites")
        empty = next((i for i in range(start + 1, end)
                      if lines[i].strip() == "[]"), None)
        if empty is not None:
            lines[empty:empty + 1] = entry_lines
        else:
            lines[end:end] = entry_lines
        return "\n".join(lines)

    def apply_data(cfg: dict):
        entry = {"id": suite_id, "file": file_rel}
        if rubric_rel:
            entry["rubric"] = rubric_rel
        cfg["suites"] = list(cfg.get("suites") or []) + [entry]

    return Op(f"declare suite {suite_id}", apply_text, apply_data,
              snippet=f"# under suites:\n{entry_text}")


def asserts_file_op(path_rel: str) -> Op:
    commented = re.compile(r"^\s*#\s*asserts_file:")

    def apply_text(text: str) -> str:
        lines = text.split("\n")
        start, end = _block_span(lines, "project")
        hit = next((i for i in range(start + 1, end) if commented.match(lines[i])), None)
        new_line = f"  asserts_file: {path_rel}"
        if hit is not None:
            lines[hit] = new_line
        else:
            lines[end:end] = [new_line]
        return "\n".join(lines)

    def apply_data(cfg: dict):
        cfg.setdefault("project", {})["asserts_file"] = path_rel

    return Op(f"set project.asserts_file = {path_rel}", apply_text, apply_data,
              snippet=f"# under project:\n  asserts_file: {path_rel}")


def wrapper_map_op(prompt_file: str, wrapper_file: str, wrappers_dir_rel: str,
                   existing_wrappers: dict | None) -> Op:
    from src.scaffold.templates import wrapper_config_block
    map_line = f"      {prompt_file}: {wrapper_file}"
    full_block = wrapper_config_block(prompt_file, wrapper_file, wrappers_dir_rel)

    def apply_text(text: str) -> str:
        lines = text.split("\n")
        start, end = _block_span(lines, "prompts")
        if existing_wrappers:
            map_at = next((i for i in range(start + 1, end)
                           if lines[i].strip() == "map:" and lines[i].startswith("    ")), None)
            if map_at is None:
                # wrappers block without a map: append one at the block end
                lines[end:end] = ["    map:", map_line]
            else:
                lines[map_at + 1:map_at + 1] = [map_line]
        else:
            lines[end:end] = full_block.split("\n")
        return "\n".join(lines)

    def apply_data(cfg: dict):
        prompts = cfg.setdefault("prompts", {})
        if existing_wrappers:
            wrappers = prompts["wrappers"]
            wrappers.setdefault("map", {})[prompt_file] = wrapper_file
        else:
            prompts["wrappers"] = {
                "dir": wrappers_dir_rel,
                "strip_frontmatter": True,
                "strip_lines_matching": [],
                "heading": "Boundary reminders (from the production wrapper)",
                "map": {prompt_file: wrapper_file},
            }

    return Op(f"map wrapper {wrapper_file} onto {prompt_file}", apply_text, apply_data,
              snippet=f"# under prompts:\n{full_block}")


@dataclass
class EditResult:
    applied: bool
    descriptions: list[str] = field(default_factory=list)
    backup: Path | None = None
    error: str | None = None
    snippet: str = ""


def apply(config_path: Path, ops: list[Op]) -> EditResult:
    """Apply all ops or none. Verifies semantics BEFORE writing; writes a
    .bak regardless; rolls back if the written file fails to reload."""
    if not ops:
        return EditResult(applied=True)
    before = config_path.read_text()
    snippet = "\n\n".join(op.snippet for op in ops if op.snippet)
    try:
        expected = yaml.safe_load(before)
        for op in ops:
            op.apply_data(expected)
        text = before
        for op in ops:
            text = op.apply_text(text)
        got = yaml.safe_load(text)
        if got != expected:
            raise ConfigEditError(
                "edited config does not parse to the expected result — refusing to write")
        config._validate(got)
    except (ConfigEditError, yaml.YAMLError, config.ConfigError) as e:
        return EditResult(applied=False, error=str(e), snippet=snippet)

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    backup.write_text(before)
    config_path.write_text(text)
    try:
        config.load(force_reload=True)
    except config.ConfigError as e:  # belt and braces: roll back
        config_path.write_text(before)
        config.load(force_reload=True)
        return EditResult(applied=False, error=f"post-write reload failed ({e}); rolled back",
                          snippet=snippet, backup=backup)
    return EditResult(applied=True, descriptions=[op.description for op in ops],
                      backup=backup)

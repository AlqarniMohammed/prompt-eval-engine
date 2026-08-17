"""Create-if-absent file emission for `prompt-eval init`.

Nothing here ever overwrites: an existing identical file is "already there",
an existing different file is kept and reported (or, for the prompt itself,
escalated by the caller as a collision). Every action is returned as a row
for the from->to table and the init ledger.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from src import config


class EmitError(Exception):
    pass


@dataclass
class Action:
    kind: str        # write | move | mkdir
    dest: Path
    status: str      # created | exists-identical | exists-kept | planned
    source: Path | None = None
    stage: str = ""   # pipeline stage the file serves
    proves: str = ""  # the teaching line


def rel(path: Path) -> str:
    """Repo-root-relative when possible (what goes into config and output)."""
    try:
        return str(Path(path).resolve().relative_to(config.ROOT))
    except ValueError:
        return str(Path(path).resolve())


def write(dest: Path, content: str, stage: str, proves: str, dry: bool) -> Action:
    dest = Path(dest)
    if dest.exists():
        status = "exists-identical" if dest.read_text() == content else "exists-kept"
        return Action("write", dest, status, stage=stage, proves=proves)
    if dry:
        return Action("write", dest, "planned", stage=stage, proves=proves)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return Action("write", dest, "created", stage=stage, proves=proves)


def move(source: Path, dest: Path, stage: str, proves: str, dry: bool) -> Action:
    """Move a dropped file into place. Identical content already at dest =
    done (source is removed); different content = collision, hard error."""
    source, dest = Path(source), Path(dest)
    if dest.exists():
        if dest.read_text() == source.read_text():
            if not dry:
                source.unlink()
            return Action("move", dest, "exists-identical", source=source,
                          stage=stage, proves=proves)
        raise EmitError(
            f"{rel(dest)} already exists with DIFFERENT content — refusing to "
            f"overwrite. Pick another suite id (--suite) or reconcile by hand.")
    if dry:
        return Action("move", dest, "planned", source=source, stage=stage, proves=proves)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return Action("move", dest, "created", source=source, stage=stage, proves=proves)


def move_tree(source: Path, dest_dir: Path, stage: str, proves: str, dry: bool) -> Action:
    """Move a support file or directory under a workspace/fixture dir."""
    source, dest_dir = Path(source), Path(dest_dir)
    dest = dest_dir / source.name
    if dest.exists():
        return Action("move", dest, "exists-kept", source=source, stage=stage, proves=proves)
    if dry:
        return Action("move", dest, "planned", source=source, stage=stage, proves=proves)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return Action("move", dest, "created", source=source, stage=stage, proves=proves)


def append_function(dest: Path, module_text: str, function_name: str,
                    stage: str, proves: str, dry: bool) -> Action:
    """Ensure the asserts module exists and defines the starter function —
    create the module if absent, append the function if the module exists
    without it, no-op if already defined."""
    dest = Path(dest)
    if not dest.exists():
        return write(dest, module_text, stage, proves, dry)
    text = dest.read_text()
    if f"def {function_name}(" in text:
        return Action("write", dest, "exists-identical", stage=stage, proves=proves)
    body_at = module_text.find(f"def {function_name}(")
    if body_at == -1:
        raise EmitError(f"template lacks def {function_name}")
    if dry:
        return Action("write", dest, "planned", stage=stage, proves=proves)
    dest.write_text(text.rstrip() + "\n\n\n" + module_text[body_at:])
    return Action("write", dest, "created", stage=stage, proves=proves)

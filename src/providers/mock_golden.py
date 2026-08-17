"""Offline provider: replays handcrafted golden bundles instead of calling
any model. GOLDEN_SET=pass replays outputs that must satisfy every
deterministic assertion; GOLDEN_SET=fail replays deliberately-violating
outputs that must each trip at least one assertion.

Golden dirs may hold phrasing-diverse variants (pass.txt, pass-alt.txt, ...).
GOLDEN_VARIANT=N selects the Nth variant (sorted); a case with fewer variants
replays its first — so validate can sweep every variant of every case
without per-case bookkeeping."""

from __future__ import annotations

import os
from pathlib import Path

from src import config


def golden_files(directory: Path, golden_set: str) -> list[str]:
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(f.name for f in d.iterdir() if f.name.startswith(golden_set) and f.name.endswith(".txt"))


def call_api(prompt: str, options: dict, context: dict) -> dict:
    variables = (context or {}).get("vars") or {}
    golden_set = os.environ.get("GOLDEN_SET", "pass")
    variant = int(os.environ.get("GOLDEN_VARIANT", "0"))
    golden = variables.get("golden")
    if not golden:
        return {"error": "test is missing vars.golden"}
    directory = config.fixtures_dir() / "golden" / str(golden)
    files = golden_files(directory, golden_set)
    if not files:
        return {"error": f"missing golden bundle: golden/{golden}/{golden_set}*.txt"}
    name = files[min(variant, len(files) - 1)]
    return {"output": (directory / name).read_text()}

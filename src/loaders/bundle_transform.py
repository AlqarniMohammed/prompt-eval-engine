"""Test-level output transform for live runs: packages the agent's final
message plus every file it created or modified in its workspace into the
plain-text bundle envelope the asserts and judges read.

Also the first line of hollow-output defense (incident #3): a Write-enabled
case that created nothing and said almost nothing is an ERROR row, never a
gradeable "success". (The afterEach hook adds the terminal-reason check —
the transform context does not carry response metadata.)"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.utils.bundle import format_bundle, to_list

MANIFEST = ".workspace-manifest.json"
HOLLOW_STDOUT_CHARS = 400


def get_transform(output, context) -> str:
    variables = (context or {}).get("vars") or {}
    workdir = variables.get("workdir")
    if not workdir:
        return output  # offline replay — goldens already ARE bundles

    workspace = Path(workdir)
    manifest_path = workspace / MANIFEST
    baseline: dict = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    stdout = output if isinstance(output, str) else json.dumps(output)
    files: dict[str, str] = {}
    for p in sorted(workspace.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(workspace))
        if rel == MANIFEST:
            continue
        try:
            content = p.read_text()
        except UnicodeDecodeError:
            continue  # binary artifacts are not bundle material
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if baseline.get(rel) != digest:  # created or modified
            files[rel] = content

    can_write = any(t in ("Write", "Edit") for t in to_list(variables.get("allowedTools")))
    if can_write and not files and len(stdout.strip()) < HOLLOW_STDOUT_CHARS:
        raise RuntimeError(
            f"hollow output: Write-enabled case produced no files and only "
            f"{len(stdout.strip())} chars of stdout — refusing to grade it "
            "(max-turns/limit exhaustion must be an error, never a cached success)"
        )
    return format_bundle(stdout, files)

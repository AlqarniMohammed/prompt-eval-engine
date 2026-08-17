"""promptfoo cache operations — tooling instead of manual purges (the JS
campaign's poisoned-cache fix was an untracked shell command, incident #3).

The cache is pinned into the repo (outputs/.promptfoo-cache) via
PROMPTFOO_CACHE_PATH so runs never share state with other projects. Entries
are cache-manager fs-hash JSON files; scanning is tolerant of anything that
does not parse."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src import config
from src.utils.bundle import parse_bundle

HOLLOW_STDOUT_CHARS = 400


def cache_dir() -> Path:
    return config.outputs_root() / ".promptfoo-cache"


def _entries():
    root = cache_dir()
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def ls() -> list[dict]:
    out = []
    for path in _entries():
        out.append({"file": str(path.relative_to(cache_dir())), "bytes": path.stat().st_size,
                    "age_days": round((time.time() - path.stat().st_mtime) / 86400, 1)})
    return out


def stat() -> dict:
    entries = ls()
    return {"entries": len(entries), "bytes": sum(e["bytes"] for e in entries),
            "dir": str(cache_dir())}


def _looks_hollow(output_text: str) -> str | None:
    parsed = parse_bundle(output_text)
    if not parsed["files"] and len(parsed["stdout"].strip()) < HOLLOW_STDOUT_CHARS \
            and "===== STDOUT =====" in output_text:
        return f"bundle with no FILE sections and {len(parsed['stdout'].strip())} chars of stdout"
    return None


def verify() -> list[dict]:
    """The hollow-entry heuristic, codified: cached responses whose output is
    an error, empty, or a fileless near-empty bundle. These are the entries
    that poisoned a verdict in the JS campaign."""
    suspects = []
    for path in _entries():
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        # cache-manager stores {key, val}; the provider response lives in val.
        value = blob.get("val") if isinstance(blob, dict) else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {"output": value}
        if not isinstance(value, dict):
            continue
        problem = None
        if value.get("error"):
            problem = f"cached ERROR response: {str(value['error'])[:80]}"
        else:
            output = value.get("output")
            if output is None or (isinstance(output, str) and not output.strip()):
                problem = "cached empty output"
            elif isinstance(output, str):
                problem = _looks_hollow(output)
        if problem:
            suspects.append({"file": str(path.relative_to(cache_dir())), "problem": problem})
    return suspects


def gc(older_than_days: float | None = None, suspects_only: bool = False) -> int:
    removed = 0
    suspect_files = {s["file"] for s in verify()} if suspects_only else None
    now = time.time()
    for path in list(_entries()):
        rel = str(path.relative_to(cache_dir()))
        if suspects_only and rel not in suspect_files:
            continue
        if older_than_days is not None and (now - path.stat().st_mtime) / 86400 < older_than_days:
            continue
        path.unlink()
        removed += 1
    return removed

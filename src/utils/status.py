"""Per-run observability: structured JSONL events in <run_dir>/status.jsonl
with a one-line human mirror on stderr. Per-run directories replace the JS
kit's single shared status file that the next run truncated (incident #10)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def emit(run_dir: Path, ev: str, **fields) -> dict:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "ev": ev, **fields}
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "status.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")
    label = f" {fields['case']}" if fields.get("case") else ""
    extra = " ".join(str(x) for x in [
        fields.get("model"),
        f"{fields['elapsedMs'] / 1000:.1f}s" if fields.get("elapsedMs") is not None else None,
        "cached" if fields.get("cached") else None,
        "FAILED" if fields.get("ok") is False else None,
        fields.get("note"),
    ] if x)
    print(f"[eval] {ev}{label} {extra}".rstrip(), file=sys.stderr)
    return record


def read(run_dir: Path) -> list[dict]:
    path = run_dir / "status.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

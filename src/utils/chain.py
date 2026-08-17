"""Hash-chained append-only ledgers — tamper-evident history.

outputs/history/ is plain files; anyone can edit them. Chaining makes edits
*evident*: each chained line stores `prev` (the hash of the line before it)
and `hash` (sha256 of the entry's canonical JSON including `prev`), so a
modified, inserted, or deleted line breaks every hash after it.

Legacy compatibility: lines written before chaining existed carry no `hash`.
They are legal only as a prefix — the first chained entry anchors them by
hashing the exact bytes that precede it (`prev: "legacy:<sha>"`), so even
the pre-chain history cannot be edited without breaking the chain.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def _sha(text_or_bytes) -> str:
    data = text_or_bytes if isinstance(text_or_bytes, bytes) else str(text_or_bytes).encode()
    return hashlib.sha256(data).hexdigest()


def _last_hash_or_anchor(path: Path) -> str:
    if not path.exists() or not path.stat().st_size:
        return "genesis"
    raw = path.read_bytes()
    for line in reversed(raw.decode().splitlines()):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and "hash" in e:
            return e["hash"]
    return "legacy:" + _sha(raw)


def append_chained(path, entry: dict) -> dict:
    """Append one entry, chained to whatever the file already holds."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {**entry, "prev": _last_hash_or_anchor(p)}
    body["hash"] = _sha(_canonical(body))
    with p.open("a") as f:
        f.write(json.dumps(body) + "\n")
    return body


def verify_chain(path) -> list[str]:
    """Walk the chain; return every break as a problem string (empty = clean).
    Unchained lines are legal only before the first chained line."""
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_bytes().decode()
    lines = raw.splitlines()
    problems: list[str] = []
    prev: str | None = None          # None until the chain starts
    offset = 0                       # byte offset of the current line
    for i, line in enumerate(lines, start=1):
        line_start = offset
        offset += len(line.encode()) + 1
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{p.name}:{i}: unparseable line"
                            + (" after the chain started" if prev is not None else ""))
            continue
        if not isinstance(e, dict) or "hash" not in e:
            if prev is not None:
                problems.append(f"{p.name}:{i}: unchained entry after the chain started")
            continue
        expected_prev = prev if prev is not None else (
            "genesis" if line_start == 0
            else "legacy:" + _sha(raw[:line_start].encode()))
        if e.get("prev") != expected_prev:
            problems.append(f"{p.name}:{i}: prev-hash mismatch (chain broken above this line)")
        body = {k: v for k, v in e.items() if k != "hash"}
        if _sha(_canonical(body)) != e["hash"]:
            problems.append(f"{p.name}:{i}: entry hash mismatch (line was modified)")
        prev = e["hash"]
    return problems

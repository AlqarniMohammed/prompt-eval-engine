"""Secret scrubbing + raw-text policy for DURABLE artifacts.

outputs/history/ is git-tracked and outlives every run; manifests embed the
verbatim config on dirty trees and compare records embed full prompt text. A
key or token pasted into any of those would be committed forever. Durable
writes therefore always pass through scrub(); per-run scratch under
outputs/runs/ is deliberately untouched (it is gitignored and observability
must show the truth).

The raw-text policy (governance.redaction.history_raw_text) additionally
lets a project hash or omit full prompt/output text in durable records:
    keep (default)  scrubbed text is stored verbatim
    hash            replaced by {"sha256": ..., "chars": ...}
    omit            dropped entirely
"""

from __future__ import annotations

import re

from src import config

SECRET_PATTERNS = [
    ("anthropic-key", r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    ("api-key", r"\bsk-[A-Za-z0-9_\-]{20,}"),
    ("aws-key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ("bearer-token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _patterns() -> list[tuple[str, str]]:
    extra = ((config.get().get("governance") or {}).get("redaction") or {}).get("patterns") or []
    return SECRET_PATTERNS + [(f"custom-{i}", p) for i, p in enumerate(extra, 1)
                              if isinstance(p, str)]


def scrub(text: str) -> tuple[str, int]:
    """Replace every secret-pattern match with «REDACTED:<label>»."""
    hits = 0
    for label, pattern in _patterns():
        text, n = re.subn(pattern, f"«REDACTED:{label}»", text)
        hits += n
    return text, hits


def durable_text(text: str | None):
    """Apply the raw-text policy + secret scrub for a durable record field."""
    if text is None:
        return None
    mode = ((config.get().get("governance") or {}).get("redaction") or {}).get(
        "history_raw_text", "keep")
    if mode == "omit":
        return None
    if mode == "hash":
        import hashlib
        return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text)}
    return scrub(text)[0]

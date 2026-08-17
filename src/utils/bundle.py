"""The plain-text envelope every provider returns:

    ===== STDOUT =====
    <the model's final message>
    ===== FILE: relative/path.md =====
    <content of a file the run created or modified>

Plain text (not JSON) so contains/regex asserts and rubric judges read it
directly, while Python asserts parse it back into parts. Domain-free by
design: prompt-specific metrics live in src/evaluators/domain/.
"""

from __future__ import annotations

import re

STDOUT_HEADER = "===== STDOUT ====="
_FILE_HEADER_RE = re.compile(r"^===== FILE: (.+?) =====$")


def format_bundle(stdout: str, files: dict[str, str] | None = None) -> str:
    parts = [STDOUT_HEADER, str(stdout or "").rstrip()]
    for rel, content in (files or {}).items():
        parts.append(f"===== FILE: {rel} =====")
        parts.append(str(content or "").rstrip())
    return "\n".join(parts) + "\n"


def parse_bundle(bundle: str) -> dict:
    out = {"stdout": "", "files": {}}
    current: str | None = None  # None | "stdout" | file path
    buf: list[str] = []

    def flush():
        if current is None:
            return
        text = "\n".join(buf).strip()
        if current == "stdout":
            out["stdout"] = text
        else:
            out["files"][current] = text
        buf.clear()

    for line in str(bundle).split("\n"):
        m = _FILE_HEADER_RE.match(line)
        if line.strip() == STDOUT_HEADER:
            flush()
            current = "stdout"
        elif m:
            flush()
            current = m.group(1).strip()
        else:
            buf.append(line)
    flush()
    return out


def passed(reason: str = "ok") -> dict:
    return {"pass": True, "score": 1, "reason": reason}


def failed(reason: str) -> dict:
    return {"pass": False, "score": 0, "reason": reason}


def to_list(value) -> list[str]:
    """List vars are comma-separated strings in the test YAMLs — promptfoo
    expands YAML-array vars into one test case per element, which is never
    what we want. Accepts a real list too, for robustness."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return [s.strip() for s in str(value or "").split(",") if s.strip()]

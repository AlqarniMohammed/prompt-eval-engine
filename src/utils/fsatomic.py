"""Atomic durable writes.

A crash mid-write must never leave a torn JSON file: a torn .state.json reads
back as "nothing ever earned" and a torn manifest loses the run's audit trail.
Temp-file-plus-rename is atomic on POSIX (os.replace), so readers see either
the old content or the new — never a prefix.
"""

import os
from pathlib import Path


def write_text_atomic(path, text: str):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)
    return p

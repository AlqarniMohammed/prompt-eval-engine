"""The engine version is declared in three places and hand-synced.

Single-sourcing was considered and rejected: hatch dynamic versioning would
cover only pyproject <-> ENGINE_VERSION, leaving package.json manual anyway,
and it touches the build system for zero behavior change. This test makes a
desync impossible to ship instead.

Python 3.10 has no tomllib, so pyproject.toml is regex-parsed.
"""

import json
import re
from pathlib import Path

from src import config

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version = "(.+)"$', text, flags=re.MULTILINE)
    assert m, "pyproject.toml has no version line"
    return m.group(1)


def _package_json_version() -> str:
    return json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]


def test_version_declarations_match():
    versions = {
        "pyproject.toml": _pyproject_version(),
        "package.json": _package_json_version(),
        "src.config.ENGINE_VERSION": config.ENGINE_VERSION,
    }
    assert len(set(versions.values())) == 1, f"version desync: {versions}"

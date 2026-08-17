"""The handbook must not lie: every incident it cites exists in
LESSONS-LEARNED.md, every dotted config key it cites resolves in the shipped
YAML (or is an optional key some code actually reads), and the 19 promised
sections are all present. Prose reasoning can't be tested — references can."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HANDBOOK = ROOT / "docs" / "HANDBOOK.md"

_FILE_EXTS = {"py", "md", "json", "jsonl", "yaml", "yml", "txt", "toml",
              "lock", "html", "js", "example"}


def _handbook_text() -> str:
    return HANDBOOK.read_text()


def test_handbook_exists_and_is_substantial():
    assert HANDBOOK.exists()
    assert len(_handbook_text().splitlines()) > 500


def test_all_19_sections_present():
    text = _handbook_text()
    for n in range(1, 20):
        assert re.search(rf"^## {n}\. ", text, re.M), f"section {n} heading missing"


def test_cited_incidents_exist_in_lessons_learned():
    lessons = (ROOT / "LESSONS-LEARNED.md").read_text()
    known = {int(m) for m in re.findall(r"^\| (\d+) \|", lessons, re.M)}
    assert known, "LESSONS-LEARNED incident table not found"
    cited = {int(m) for m in re.findall(r"incident[s]? #(\d+)", _handbook_text(), re.I)}
    assert cited, "handbook cites no incidents — the post-mortem is its spine"
    unknown = cited - known
    assert not unknown, f"handbook cites incidents that do not exist: {sorted(unknown)}"


def _dotted_config_keys() -> list[str]:
    keys = []
    for span in re.findall(r"`([^`]+)`", _handbook_text()):
        token = span.strip()
        if "/" in token or " " in token or ":" in token:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", token):
            continue
        if token.rsplit(".", 1)[-1] in _FILE_EXTS:
            continue
        keys.append(token)
    return keys


def test_cited_config_keys_resolve():
    cfg = yaml.safe_load((ROOT / "config" / "eval_config.yaml").read_text())
    src_text = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py"))
    keys = _dotted_config_keys()
    assert keys, "handbook cites no config keys — implausible"
    for key in keys:
        node = cfg
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is not None:
            continue  # resolves in the shipped YAML
        # optional keys (documented but commented out) must at least be read
        # by code — a key nobody declares AND nobody reads is fiction
        leaf = key.rsplit(".", 1)[-1]
        assert f'"{leaf}"' in src_text or f"'{leaf}'" in src_text, (
            f"handbook cites `{key}` but it neither resolves in "
            f"eval_config.yaml nor appears in src/")


def test_toc_anchors_match_headings():
    text = _handbook_text()
    headings = re.findall(r"^## (\d+\. .+)$", text, re.M)
    assert len(headings) == 19
    for h in headings:
        anchor = re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        assert f"(#{anchor})" in text, f"TOC link missing/mismatched for: {h}"

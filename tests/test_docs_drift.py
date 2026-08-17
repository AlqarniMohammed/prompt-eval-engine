"""Docs drift check (incident #13: METHOD.md drifted to wrong paths, phantom
env vars, and a config header claiming "No API keys" — guardrail commits never
updated docs). Every path, EVAL_* env var, CLI command, and --flag that the
docs put in backticks must exist in the code, and every CLI subcommand must be
documented.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# CHANGELOG.md is deliberately NOT scanned: history must be allowed to name
# files and flags that no longer exist.
DOCS = [ROOT / "README.md", ROOT / "RUNBOOK.md",
        ROOT / "examples" / "support-reply" / "WALKTHROUGH.md",
        *sorted((ROOT / "docs").glob("*.md"))]

# Working files created at runtime — legitimately documented, absent on a
# fresh clone.
RUNTIME_PATHS = {"outputs/.state.json", "outputs/.state.backup.json",
                 "outputs/runs/", "datasets/generated/", ".env",
                 "outputs/history/", "outputs/history/spend-ledger.jsonl",
                 "outputs/history/blocked-spend.jsonl",
                 "outputs/history/spot-checks.jsonl",
                 "outputs/.state-snapshots/"}


def _backticked_tokens():
    tokens = []
    for doc in DOCS:
        for span in re.findall(r"`([^`]+)`", doc.read_text()):
            tokens.append((doc.name, span))
    return tokens


def _parser():
    from src.runner import build_parser
    return build_parser()


def _subcommands(parser):
    for action in parser._subparsers._group_actions:
        return dict(action.choices)
    raise AssertionError("runner parser has no subcommands")


def test_docs_exist():
    for doc in DOCS:
        assert doc.exists(), f"{doc.name} missing"


def test_documented_paths_exist():
    prefixes = ("src/", "config/", "datasets/", "judges/", "prompts/",
                "fixtures/", "outputs/", "tests/", "input/", "docs/",
                "examples/")
    for doc, span in _backticked_tokens():
        token = span.strip()
        if not token.startswith(prefixes) or " " in token:
            continue
        if token in RUNTIME_PATHS:
            continue
        assert (ROOT / token.rstrip("/")).exists(), f"{doc}: `{token}` does not exist"


def test_documented_env_vars_exist_in_code():
    src_text = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py"))
    for doc, span in _backticked_tokens():
        for var in re.findall(r"\bEVAL_[A-Z_]+\b", span):
            assert var in src_text, f"{doc}: `{var}` not used anywhere in src/"


def test_documented_flags_exist_in_cli():
    known_flags = set()
    for sub in _subcommands(_parser()).values():
        for action in sub._actions:
            known_flags.update(action.option_strings)
    for doc, span in _backticked_tokens():
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]+", span):
            assert flag in known_flags, f"{doc}: `{flag}` is not a CLI flag"


def test_every_subcommand_is_documented_in_readme():
    readme = (ROOT / "README.md").read_text()
    for name in _subcommands(_parser()):
        assert f"`{name}`" in readme, f"README does not document `{name}`"


def test_documented_runner_invocations_are_real_subcommands():
    subs = set(_subcommands(_parser()))
    for doc, span in _backticked_tokens():
        for cmd in re.findall(r"(?:runner\.py|prompt-eval) (\w[\w-]*)", span):
            assert cmd in subs, f"{doc}: `{cmd}` is not a subcommand"


def test_config_models_match_readme_claims():
    # README names the default models; they must match the declaration file.
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "eval_config.yaml").read_text())
    readme = (ROOT / "README.md").read_text()
    for model in (cfg["agents"]["generation"]["model"], cfg["agents"]["judge"]["model"]):
        assert model in readme, f"README does not mention configured model {model}"

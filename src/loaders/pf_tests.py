"""promptfoo test generators (`tests: file://src/loaders/pf_tests.py:<fn>`).

Offline (EVAL_OFFLINE=1): cases pass through untouched — the config-level
mock provider replays golden bundles; no workspaces, no trials.

Live: each case is expanded to case x trial (x prompt-variant in compare
mode), and every expansion gets
  - its OWN fresh fixture workspace under $EVAL_RUN_DIR/work/ (the JS kit's
    per-call temp dirs, made explicit — trials must never see each other's
    files),
  - per-test agent-sdk config (workspace, model/caps, env cache-salt per
    trial) carried in `options:` — NOT `provider:`. promptfoo never loads a
    test-level provider dict coming from a Python generator
    (readPythonTestCases skips readTest, and the evaluator's
    isApiProvider(test.provider) check then silently falls back to the
    config-level provider). `options` is the channel promptfoo actually
    honors: mergeProviderPromptConfig spreads test.options into
    prompt.config, which callApi merges over the provider config. The
    config-level provider carries a sentinel working_dir so any test that
    loses this override fails loudly and free,
  - the bundle transform that packages created files into the STDOUT+FILE
    envelope the asserts and judges read.

Trial expansion is explicit tests (not promptfoo --repeat) precisely so each
trial owns a workspace and a distinct cache identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from src import config
from src.utils.bundle import to_list
from src.utils.dataset_loader import load_cases, load_graded_cases

TRANSFORM = "file://src/loaders/bundle_transform.py"
WRAPUP_KILLER = (
    "The moment the final deliverable file has been written, end the session "
    "with a single short confirmation line. Do not re-read, summarize, or "
    "review the artifact you just wrote."
)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(text)).strip("-")[:80]


def _run_dir() -> Path:
    d = os.environ.get("EVAL_RUN_DIR")
    if not d:
        raise RuntimeError(
            "EVAL_RUN_DIR is not set — live runs must go through runner.py "
            "(direct `promptfoo eval` invocations bypass the ledger, lock, and budget gate)"
        )
    return Path(d)


def _materialize_workspace(case: dict, trial: int, variant: str | None) -> Path:
    """Copy the case's fixture dir(s) into a fresh per-trial workspace and
    record the pre-existing file inventory for the bundle transform."""
    variables = case["vars"]
    name = _slug(variables.get("cell") or case.get("description") or variables.get("golden") or "case")
    parts = [name] + ([variant] if variant else []) + [f"t{trial}"]
    workspace = _run_dir() / "work" / "-".join(parts)
    workspace.mkdir(parents=True, exist_ok=True)
    for fixture in to_list(variables.get("fixture")):
        src = config.fixtures_dir() / fixture
        if not src.is_dir():
            raise FileNotFoundError(f"fixture missing: fixtures/{fixture}")
        shutil.copytree(src, workspace, dirs_exist_ok=True)
    manifest = {
        str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(workspace.rglob("*")) if p.is_file()
    }
    (workspace / ".workspace-manifest.json").write_text(json.dumps(manifest, indent=2))
    _pin_mtimes(workspace)
    return workspace


# Fixed base for content-derived mtimes (any constant in the past works).
_MTIME_EPOCH = 1_600_000_000


def _pin_mtimes(workspace: Path) -> None:
    """promptfoo's generation-cache fingerprint hashes file MTIMES, not
    contents — freshly copied workspaces would therefore never cache-hit
    across runs. Pin every file's mtime to a value derived from its content
    hash (so the fingerprint still changes iff content changes) and pin
    directory mtimes last (they move when children are written)."""
    for p in sorted(workspace.rglob("*"), reverse=True):
        if p.is_file():
            t = _MTIME_EPOCH + int(hashlib.sha256(p.read_bytes()).hexdigest()[:8], 16) % 10_000_000
            os.utime(p, (t, t))
        elif p.is_dir():
            os.utime(p, (_MTIME_EPOCH, _MTIME_EPOCH))
    os.utime(workspace, (_MTIME_EPOCH, _MTIME_EPOCH))


def _provider_options(case: dict, trial: int, workspace: Path) -> dict:
    """Per-test agent-sdk config keys, delivered via test `options` (see
    module docstring for why `provider:` doesn't work from Python)."""
    gen = config.agents()["generation"]
    provider_config: dict = {
        "model": gen["model"],
        "working_dir": str(workspace),
        "max_turns": gen.get("max_turns", 40),
        "max_budget_usd": gen.get("max_budget_usd", 3),
        "append_system_prompt": WRAPUP_KILLER,
        "env": {
            # Distinct cache identity per trial (the JS kit's trial salt).
            "EVAL_TRIAL": str(trial),
            # Per-turn output cap — 64k: smaller caps truncated document
            # suites mid-write and re-billed the whole document (incident #5).
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(gen.get("max_tokens_per_turn", 64000)),
            # The provider hashes its merged env into the cache key; pin the
            # per-run dir to a constant there or no run ever cache-hits the
            # last. (The SDK subprocess doesn't use it — hooks and loaders
            # read it from the promptfoo process env, which keeps the real
            # value.)
            "EVAL_RUN_DIR": "",
        },
    }
    tools = to_list(case["vars"].get("allowedTools"))
    if tools:
        provider_config["custom_allowed_tools"] = tools
    return provider_config


def _absolutize_assert_refs(case: dict) -> dict:
    fixed = []
    for a in case.get("assert") or []:
        value = a.get("value")
        if (isinstance(value, str) and value.startswith("file://")
                and not value.startswith("file:///")):
            a = {**a, "value": f"file://{config.ROOT}/{value[len('file://'):]}"}
        fixed.append(a)
    return {**case, "assert": fixed}


def _expand(cases: list[dict], repeat: int, variants: list[str] | None) -> list[dict]:
    if os.environ.get("EVAL_OFFLINE") == "1":
        return cases
    # http-provider runs (runner sets EVAL_HTTP_PROVIDER): the endpoint gets
    # the rendered prompt and returns text — no workspace, no agent-sdk
    # options, no bundle transform. The config-level provider IS the wiring.
    http_mode = os.environ.get("EVAL_HTTP_PROVIDER") == "1"
    if http_mode:
        # The materialized http config lives in the RUN DIR, and promptfoo
        # resolves relative file:// assert refs against the config's own
        # directory — absolutize them or every contract errors FileNotFound.
        cases = [_absolutize_assert_refs(c) for c in cases]
    out = []
    for case in cases:
        for variant in (variants or [None]):
            for trial in range(1, repeat + 1):
                suffix = (f" [{variant}]" if variant else "") + (f" #t{trial}" if repeat > 1 else "")
                expanded = {
                    **case,
                    "description": f"{case.get('description', 'case')}{suffix}",
                    "vars": {
                        **case["vars"],
                        "trial": str(trial),
                        **({"promptVariant": variant} if variant else {}),
                    },
                }
                if not http_mode:
                    workspace = _materialize_workspace(case, trial, variant)
                    expanded["vars"]["workdir"] = str(workspace)
                    expanded["options"] = {
                        **(case.get("options") or {}),
                        "transform": TRANSFORM,
                        **_provider_options(case, trial, workspace),
                    }
                out.append(expanded)
    return out


def _repeat() -> int:
    return int(os.environ.get("EVAL_REPEAT") or config.agents()["generation"].get("repeat", 1))


def _graded_repeat(cases: list[dict]) -> dict[str, int]:
    """Per-case k under adaptive-k: ceiling-pinned screen suites run k=1.
    Screen-only — no escalation pass exists; a flagged screen result warrants
    a manual full-k re-run (EVAL_ADAPTIVE_K=0 disables screening)."""
    graded = config.get().get("graded", {})
    k = int(os.environ.get("EVAL_REPEAT") or graded.get("repeat", 3))
    adaptive = graded.get("adaptive_k", {})
    screen = set(adaptive.get("screen_suites", [])) if adaptive.get("enabled") else set()
    return {
        id(case): 1 if (case["vars"]["suite"] in screen and os.environ.get("EVAL_ADAPTIVE_K", "1") == "1") else k
        for case in cases
    }


def generate_tests() -> list[dict]:
    """Binary tier (validate / baseline)."""
    return _expand(load_cases(), _repeat(), None)


def generate_compare_tests() -> list[dict]:
    """Binary compare: current vs candidate on the same cases."""
    return _expand(load_cases(), _repeat(), ["current", "candidate"])


def _with_graded_judge(cases: list[dict]) -> list[dict]:
    judge_assert = {"type": "python", "value": "file://src/evaluators/graded_judge.py:graded_judge_assert"}
    return [{**c, "assert": list(c.get("assert") or []) + [judge_assert]} for c in cases]


def _expand_graded(variants: list[str] | None) -> list[dict]:
    cases = _with_graded_judge(load_graded_cases())
    if os.environ.get("EVAL_OFFLINE") == "1":
        return cases
    per_case_k = _graded_repeat(cases)
    out = []
    for case in cases:
        out += _expand([case], per_case_k[id(case)], variants)
    return out


def generate_graded_tests() -> list[dict]:
    return _expand_graded(None)


def generate_compare_graded_tests() -> list[dict]:
    return _expand_graded(["current", "candidate"])

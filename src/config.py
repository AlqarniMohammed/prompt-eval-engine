"""Sole source of project configuration for every engine module.

Loads and validates config/eval_config.yaml (version 2), resolves declared
paths against the repo root, and applies env-var overrides on top of the
agents: block. Every override actually used is reported via overrides_used()
so the run manifest can record it (post-mortem incident #11: 40/41 paid runs
had unrecoverable effective configs).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ENGINE_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parents[1]

# Env overrides the engine honors, mapped to the (agent, key) they patch.
_ENV_OVERRIDES = {
    "EVAL_MODEL": ("generation", "model"),
    "EVAL_MAX_BUDGET_USD": ("generation", "max_budget_usd"),
    "EVAL_REPEAT": ("generation", "repeat"),
    "EVAL_JUDGE_MODEL": ("judge", "model"),
}

_cached: dict | None = None


class ConfigError(Exception):
    pass


def _fail(msg: str):
    raise ConfigError(f"eval_config.yaml: {msg}")


def config_path() -> Path:
    override = os.environ.get("EVAL_CONFIG")
    if override:
        p = Path(override).resolve()
        if not p.exists():
            _fail(f"$EVAL_CONFIG points at a missing file: {p}")
        return p
    return ROOT / "config" / "eval_config.yaml"


def _require(cfg: dict, key_path: str, typ):
    value = cfg
    for k in key_path.split("."):
        value = value.get(k) if isinstance(value, dict) else None
        if value is None:
            _fail(f'missing required key "{key_path}"')
    if not isinstance(value, typ):
        _fail(f'"{key_path}" must be {typ.__name__}, got {type(value).__name__}')
    return value


def _validate(cfg: dict):
    if cfg.get("version") != 2:
        _fail(f'unsupported "version": {cfg.get("version")} (expected 2)')
    _require(cfg, "project.name", str)
    _require(cfg, "prompts.production_dir", str)
    suites = _require(cfg, "suites", list)
    seen = set()
    for i, s in enumerate(suites):
        for key in ("id", "file"):
            if not isinstance(s.get(key), str) or not s[key]:
                _fail(f'"suites[{i}].{key}" must be a non-empty string')
        if s["id"] in seen:
            _fail(f'duplicate suite id "{s["id"]}"')
        seen.add(s["id"])
    for i, s in enumerate(suites):
        if "max_run_cost_usd" in s and not isinstance(s["max_run_cost_usd"], (int, float)):
            _fail(f'"suites[{i}].max_run_cost_usd" must be a number')
        provider = s.get("provider")
        if provider is not None:
            if not isinstance(provider, dict) or provider.get("type") != "http":
                _fail(f'"suites[{i}].provider" must be a mapping with type: http '
                      "(the only supported per-suite provider)")
            url = provider.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                _fail(f'"suites[{i}].provider.url" must be an http(s):// URL')
    _require(cfg, "agents.generation.model", str)
    _require(cfg, "agents.judge.model", str)
    _require(cfg, "governance.max_run_cost_usd", (int, float))
    gov = cfg.get("governance") or {}
    role_caps = gov.get("max_cost_usd")
    if role_caps is not None:
        if not isinstance(role_caps, dict):
            _fail('"governance.max_cost_usd" must be a mapping of role -> USD')
        for role, v in role_caps.items():
            if role not in ("generation", "judge", "dataset"):
                _fail(f'"governance.max_cost_usd.{role}" — unknown role (generation/judge/dataset)')
            if not isinstance(v, (int, float)):
                _fail(f'"governance.max_cost_usd.{role}" must be a number')
    for key in ("max_daily_usd", "max_weekly_usd"):
        if key in gov and not isinstance(gov[key], (int, float)):
            _fail(f'"governance.{key}" must be a number')
    redaction = gov.get("redaction")
    if redaction is not None:
        if not isinstance(redaction, dict):
            _fail('"governance.redaction" must be a mapping')
        mode = redaction.get("history_raw_text", "keep")
        # A typo here would silently fall back to verbatim storage in the
        # git-tracked outputs/history/ — fail loudly instead.
        if mode not in ("keep", "hash", "omit"):
            _fail(f'"governance.redaction.history_raw_text" must be keep, hash, '
                  f'or omit, got "{mode}"')
        patterns = redaction.get("patterns")
        if patterns is not None and (not isinstance(patterns, list)
                                     or not all(isinstance(p, str) for p in patterns)):
            _fail('"governance.redaction.patterns" must be a list of regex strings')
    _require(cfg, "pricing", dict)


def load(force_reload: bool = False) -> dict:
    global _cached
    if _cached is not None and not force_reload:
        return _cached
    path = config_path()
    if not path.exists():
        _fail(f"not found at {path}")
    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict):
        _fail("file is empty or not a mapping")
    _validate(cfg)
    _cached = cfg
    return cfg


def get() -> dict:
    return load()


def resolve(rel: str | Path) -> Path:
    return (ROOT / rel).resolve()


def display_path(path: str | Path) -> str:
    """ROOT-relative when inside the repo, absolute otherwise — for printed
    paths (a project living outside the repo via EVAL_CONFIG must never crash
    a report line on relative_to)."""
    p = Path(path).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def load_env_file():
    """Load simple KEY=value lines from a gitignored .env at the repo root.

    Existing environment variables always win. API-key transport only.
    No early-out on ANTHROPIC_API_KEY: a cross-family judge needs the other
    provider's key loaded too, and the parse is idempotent.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def agents() -> dict:
    """agents: block with env overrides applied. Judge == generation model is
    a hard error (self-grading bias shipped a whole milestone as a buried
    warning in the JS kit)."""
    a = get()["agents"]
    generation = dict(a["generation"])
    judge = dict(a["judge"])
    for env_key, (agent, key) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        target = generation if agent == "generation" else judge
        target[key] = float(raw) if key.endswith("_usd") else (int(raw) if key == "repeat" else raw)
    if judge["model"] == generation["model"]:
        raise ConfigError(
            f'judge model equals generation model ("{judge["model"]}") — '
            "self-grading is hard-blocked; configure an independent judge"
        )
    return {"generation": generation, "judge": judge}


def overrides_used() -> dict:
    """Every honored env override present in this process's environment —
    recorded verbatim in the run manifest."""
    tracked = list(_ENV_OVERRIDES) + [
        "EVAL_CONFIG", "EVAL_SUITE", "EVAL_HOLDOUT",
        "EVAL_CAL_SAMPLES", "GOLDEN_SET", "GOLDEN_VARIANT",
        "MOCK_JUDGE", "MOCK_GRADED_JUDGE", "MOCK_PAIRWISE", "MOCK_DATASET",
    ]
    return {k: os.environ[k] for k in tracked if k in os.environ}


def suites() -> list[dict]:
    return get()["suites"]


def suite_by_id(suite_id: str) -> dict | None:
    return next((s for s in get()["suites"] if s["id"] == suite_id), None)


def pricing(model: str) -> dict:
    """USD per MTok for a model. Unknown models fall back to the MOST
    EXPENSIVE configured row so a mispriced model can only over-count.
    Non-dict rows (metadata like pricing.last_verified) are never models."""
    table = get()["pricing"]
    row = table.get(model)
    if isinstance(row, dict):
        return row
    rows = [r for r in table.values() if isinstance(r, dict)]
    if not rows:
        raise ConfigError("eval_config.yaml: pricing declares no model rows")
    return max(rows, key=lambda r: r.get("output", 0))


def outputs_dir() -> Path:
    return resolve(get().get("observability", {}).get("runs_dir", "outputs/runs"))


def history_dir() -> Path:
    return resolve(get().get("observability", {}).get("history_dir", "outputs/history"))


# Directory accessors for the project layer — defaults mirror this repo's
# layout so a third-party config may omit the paths: block entirely.
def fixtures_dir() -> Path:
    return resolve(get().get("paths", {}).get("fixtures_dir", "fixtures"))


def graded_dir() -> Path:
    return resolve(get().get("paths", {}).get("graded_dir", "datasets/graded"))


def specs_dir() -> Path:
    return resolve(get().get("graded", {}).get("specs_dir", "datasets/graded-specs"))


def regression_dir() -> Path:
    """Pinned once-failed cases; default lives beside the graded matrices so
    a relocated project layer keeps them together."""
    declared = get().get("paths", {}).get("regression_dir")
    return resolve(declared) if declared else graded_dir().parent / "regression"


def outputs_root() -> Path:
    return resolve(get().get("paths", {}).get("outputs_dir", "outputs"))


def asserts_file() -> Path:
    """The project's contract-checks module. `init` scaffolds it at
    project/asserts.py (bundles keep their own filename); the path is part of
    the validate fingerprint."""
    return resolve(get().get("project", {}).get("asserts_file", "project/asserts.py"))

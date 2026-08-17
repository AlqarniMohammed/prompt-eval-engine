"""The engine CLI — every eval run is a `promptfoo eval`; this file owns
everything promptfoo verifiably lacks: the run lock, the measured budget
gate, the spend/failure watchdog, per-run directories with crash-proof
manifests, the pipeline state machine, and the reports.

    init       adopt a dropped prompt/bundle from input/ into a wired suite (free)
    validate   offline gate: declaration checks + golden sweeps (free)
    doctor     environment sanity (free)
    preflight  real-config smoke: configured model + caps (~$0.05)
    baseline   binary-tier live run
    graded     graded-tier matrix run (k trials, judge-scored)
    compare    graded A/B current vs candidate
    confirm    post-promotion holdout confirmation
    verdict    blinded pairwise verdict over a compare results file
    calibrate  judge calibration vs golden fixtures
    matrix     regenerate graded matrices from specs
    promote    apply a PROMOTE verdict (candidate -> production)
    round      pipeline status + the next required action
    gen-cases  draft ordinary-user test cases with the cheap dataset model
    graft      restore wiped state whose fingerprints still match
    report     regenerate reports for a run dir
    dashboard  local read-only dashboard (spend, pipeline, live runs, trends)
    cache      ls | stat | verify | gc [--older-than D] [--suspects]

Every live command is behind the $1.00 default ceiling; raising it is an
explicit, recorded --max-cost.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config, state
from src.reports import checks, parse_results, summary
from src.utils import cache_tools, cost_tracker, dataset_loader, fsatomic, proc, run_lock, status


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _promptfoo_bin() -> str:
    local = config.ROOT / "node_modules" / ".bin" / "promptfoo"
    if not local.exists():
        sys.exit("promptfoo is not installed — run: npm ci")
    return str(local)


# The agent-sdk provider hashes the promptfoo process env into its cache
# key, so the child env must be DETERMINISTIC: an allowlist, not the whole
# shell env (SHLVL/TERM/OLDPWD churn would silently kill every cache hit).
_CHILD_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
                   "LANG", "LC_ALL", "TZ", "NODE_EXTRA_CA_CERTS",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
_CHILD_ENV_KEEP_PREFIXES = ("EVAL_", "PROMPTFOO_", "ANTHROPIC_", "CLAUDE_", "MOCK_")


def _base_env(run_dir: Path | None = None, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k in _CHILD_ENV_KEEP or k.startswith(_CHILD_ENV_KEEP_PREFIXES)}
    env.update({
        "PROMPTFOO_DISABLE_TELEMETRY": "1",
        "PROMPTFOO_DISABLE_UPDATE": "1",
        "PROMPTFOO_CACHE_PATH": str(config.outputs_root() / ".promptfoo-cache"),
        "PROMPTFOO_DISABLE_SHARE": "1",
        # Deterministic import root for every python-shell entry file.
        "PYTHONPATH": str(config.ROOT),
    })
    venv_python = config.ROOT / ".venv" / "bin" / "python"
    env.setdefault("PROMPTFOO_PYTHON",
                   str(venv_python) if venv_python.exists() else sys.executable)
    if run_dir is not None:
        env["EVAL_RUN_DIR"] = str(run_dir)
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def _git(args: list[str]) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _build_manifest(mode: str, suite: str | None, cases: list[dict], repeat,
                    concurrency: int, forced_gates: list[str]) -> dict:
    cfg = config.get()
    agents = config.agents()
    prompts_dir = config.resolve(cfg["prompts"]["production_dir"])
    from src.loaders.pf_prompts import prompt_sha
    prompt_shas = {
        f: prompt_sha(prompts_dir, f)
        for f in sorted({c["vars"].get("promptFile") for c in cases if c["vars"].get("promptFile")})
    }
    wrappers = cfg["prompts"].get("wrappers") or {}
    wrapper_shas = {
        w: state.sha256_file(config.resolve(wrappers["dir"]) / w)
        for w in sorted(set((wrappers.get("map") or {}).values()))
    } if wrappers else {}
    rubric_shas = {s["id"]: state.sha256_file(config.resolve(s["rubric"]))
                   for s in config.suites() if s.get("rubric")}
    git_dirty = bool(_git(["status", "--porcelain"]))
    promptfoo_version = json.loads(
        (config.ROOT / "node_modules" / "promptfoo" / "package.json").read_text())["version"]
    calibration = state.suite_state(suite).get("calibrated") if suite else None
    manifest = {
        "runId": f"{mode}-{_stamp()}",
        "mode": mode,
        "suite": suite,
        "project": cfg["project"]["name"],
        "gitSha": _git(["rev-parse", "HEAD"]),
        "gitDirty": git_dirty,
        "promptSha256": prompt_shas,
        "wrapperSha256": wrapper_shas,
        "rubricSha256": rubric_shas,
        "configSha256": state.sha256_file(config.config_path()),
        "engineVersion": config.ENGINE_VERSION,
        "promptfooVersion": promptfoo_version,
        "transport": "api",
        "models": {"generation": agents["generation"]["model"], "judge": agents["judge"]["model"]},
        "effectiveAgents": agents,
        "envOverrides": config.overrides_used(),
        "forcedGates": forced_gates,
        "calibrationRecord": (calibration or {}).get("record"),
        "repeat": repeat,
        "concurrency": concurrency,
        "cases": len(cases),
        # messages: cases are a single-prompt SIMULATION of prior turns — the
        # flag keeps every downstream reader honest about what was measured.
        "simulatedMultiTurn": any("simulatedTranscript" in (c.get("vars") or {})
                                  for c in cases),
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }
    if git_dirty:
        from src.utils import redact
        text = redact.scrub(config.config_path().read_text())[0]
        # http-provider header values are credentials by construction — mask
        # them even when they don't match a known secret pattern.
        for s in config.suites():
            for v in ((s.get("provider") or {}).get("headers") or {}).values():
                if isinstance(v, str) and v:
                    text = text.replace(v, "«redacted»")
        manifest["verbatimConfig"] = text
    return manifest


def _write_manifest(run_dir: Path, manifest: dict):
    fsatomic.write_text_atomic(run_dir / "manifest.json", json.dumps(manifest, indent=2))


# --------------------------------------------------------------- validate —
def cmd_validate(args) -> int:
    print("== 1/4 declaration + fixture checks ==")
    errors, warnings = checks.run_checks()
    for w in warnings:
        print(f" WARN {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        print(f"\n{len(errors)} check(s) FAILED")
        return 1
    print("  all declarations resolve")

    if not config.suites():
        print("\nNo suites declared yet — nothing to sweep. Drop a prompt into input/ "
              "and run `prompt-eval init` (it scaffolds and wires everything), then re-run.")
        return 0

    run_dir = config.outputs_dir() / f"validate-{_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    pf = _promptfoo_bin()

    def sweep(step: str, golden_set: str, mock_judge: str, mode: str):
        golden_root = config.fixtures_dir() / "golden"
        n_variants = 1
        if golden_root.exists():
            from src.providers.mock_golden import golden_files
            n_variants = max(
                (len(golden_files(d, golden_set)) for d in golden_root.iterdir() if d.is_dir()),
                default=1) or 1
        for variant in range(n_variants):
            if n_variants > 1:
                print(f"-- {golden_set} variant {variant + 1}/{n_variants}")
            out_file = run_dir / f"offline-{golden_set}-{mock_judge}.json"
            env = _base_env(
                run_dir, EVAL_OFFLINE="1", GOLDEN_SET=golden_set,
                GOLDEN_VARIANT=str(variant), MOCK_JUDGE=mock_judge,
                EVAL_SUITE=args.suite,
            )
            res = subprocess.run(
                [pf, "eval", "-c", "promptfooconfig.offline.yaml", "--no-cache",
                 "--no-progress-bar", "-j", "8", "-o", str(out_file)],
                cwd=config.ROOT, env=env, capture_output=True, text=True)
            if not out_file.exists():
                print(res.stdout[-2000:] if res.stdout else "")
                print(res.stderr[-2000:] if res.stderr else "", file=sys.stderr)
                sys.exit(f"promptfoo produced no results file ({step}) — real crash")
            checked, problems = parse_results.check(out_file, mode)
            for p in problems:
                print(p)
            if mode == "expect-rubric-fail" and checked == 0:
                print("WARN  no rubric-bearing cases in this suite — this sweep "
                      "proved nothing (binary contracts only; the graded tier is "
                      "where the rubric runs)")
            else:
                print(f"{checked} cases checked ({mode}): {checked - len(problems)} "
                      f"as expected, {len(problems)} not")
            if problems:
                sys.exit(1)

    print("\n== 2/4 golden PASS set (every variant must satisfy every assertion) ==")
    sweep("pass", "pass", "pass", "expect-all-pass")
    print("\n== 3/4 golden FAIL set (every variant must be caught) ==")
    sweep("fail", "fail", "pass", "expect-all-fail")
    print("\n== 4/4 rubric wiring (MOCK_JUDGE=fail must fail every rubric-bearing case) ==")
    sweep("rubric", "pass", "fail", "expect-rubric-fail")

    for suite in config.suites():
        if args.suite and suite["id"] != args.suite:
            continue
        state.record(suite["id"], "validated", state.validate_fingerprint(suite["id"]))
    print("\nOFFLINE VALIDATION GREEN — the suite is proven without a single model call.")
    first = args.suite or (config.suites()[0]["id"] if config.suites() else None)
    if first:
        st = state.suite_state(first)
        nxt = next((s for s in state.STAGES if s not in st), None)
        if nxt:
            argv, note = _next_action(first, nxt, bool(state.check_smoked(first)))
            print("Next: prompt-eval " + " ".join(argv) + (f"  {note}" if note else ""))
    return 0


# ------------------------------------------------------------------- init —
def _confirm_kind(det, args):
    """Resolve the one ambiguous detection (bare prompt vs agent prompt) with
    at most one question; --yes accepts the default, non-TTY requires --as."""
    from src.scaffold.register import InitError
    if det.kind:
        return det
    if args.yes:
        det.kind = det.default_kind
        return det
    if not sys.stdin.isatty():
        raise InitError(
            f"ambiguous input ({'; '.join(det.reasons)}) — non-interactive runs "
            f"must pass --as {det.default_kind} (or --yes to accept that default)")
    default_char = "a" if det.default_kind == "agent" else "p"
    ans = input(f"{det.source.name}: looks like an AGENT prompt "
                f"({'; '.join(det.reasons)}). Scaffold as [a]gent or bare "
                f"[p]rompt? [{default_char}] ").strip().lower()
    det.kind = ("agent" if ans.startswith("a") else
                "prompt" if ans.startswith("p") else det.default_kind)
    return det


def cmd_init(args) -> int:
    """Adopt dropped inputs: detect, scaffold (valid on arrival), register."""
    from src.scaffold import detect as detect_mod
    from src.scaffold import register
    from src.scaffold.detect import DetectError

    if getattr(args, "example", None):
        if args.path:
            print("INIT REFUSED — --example and a path are mutually exclusive", file=sys.stderr)
            return 1
        src_dir = config.ROOT / "examples" / args.example / "bundle"
        if not src_dir.is_dir():
            examples_root = config.ROOT / "examples"
            available = sorted(d.name for d in examples_root.iterdir()
                               if (d / "bundle").is_dir()) if examples_root.is_dir() else []
            print(f'INIT REFUSED — no example "{args.example}" '
                  f"(available: {', '.join(available) or 'none'})", file=sys.stderr)
            return 1
        dest = register.input_dir() / args.example
        if dest.exists():
            print(f"INIT REFUSED — {config.display_path(dest)} already exists; "
                  "remove it (or finish adopting it) first", file=sys.stderr)
            return 1
        shutil.copytree(src_dir, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        print(f"staged examples/{args.example}/bundle → {config.display_path(dest)}")
        items = [dest]
    elif args.path:
        items = [Path(args.path)]
        if not items[0].exists():
            print(f"INIT REFUSED — {args.path} does not exist", file=sys.stderr)
            return 1
    else:
        idir = register.input_dir()
        items = sorted(p for p in idir.iterdir()
                       if not p.name.startswith(".") and p.name != "README.md"
                       ) if idir.exists() else []
        if not items:
            print(f"input/ is empty — drop a prompt .md (or a folder) into "
                  f"{config.display_path(idir)} "
                  "and re-run. input/README.md lists everything init understands.")
            return 0
    if args.suite and len(items) > 1:
        print("INIT REFUSED — --suite only applies to a single item", file=sys.stderr)
        return 1

    rc = 0
    for item in items:
        try:
            det = detect_mod.detect(item, as_kind=args.as_kind, suite=args.suite)
            det = _confirm_kind(det, args)
            register.process(det, dry_run=args.dry_run, print_config=args.print_config,
                             rubric_kind=args.rubric)
        except (DetectError, register.InitError) as e:
            print(f"INIT REFUSED — {item.name}: {e}", file=sys.stderr)
            rc = 1
    if rc == 0 and not args.dry_run:
        print("\nvalidate should be green right now: uv run prompt-eval validate")
    return rc


# ----------------------------------------------------------------- doctor —
def _judge_provider_problems() -> list[str]:
    """A cross-family openai:* judge needs its key + SDK before any paid call
    (incident #9: prove the REAL configuration, not just the anthropic half).
    Mirrors model_client's provider dispatch."""
    try:
        judge_model = config.agents()["judge"]["model"]
    except config.ConfigError:
        return []  # the agents-block check reports this already
    if not judge_model.startswith("openai:"):
        return []
    problems = []
    try:
        import openai  # noqa: F401
    except ImportError:
        problems.append(f'judge "{judge_model}" needs the openai SDK — install the '
                        "extra: uv sync --extra cross-judge")
    if not os.environ.get("OPENAI_API_KEY"):
        problems.append(f'judge "{judge_model}" needs OPENAI_API_KEY (.env or environment)')
    return problems


def _pricing_staleness_warning(max_age_days: int = 180) -> str | None:
    """Stale prices skew every estimate silently — surface age, never crash."""
    stamp = (config.get().get("pricing") or {}).get("last_verified")
    if not stamp:
        return ("pricing.last_verified is not set — stamp the date the pricing "
                "table was last checked against the provider")
    try:
        verified = datetime.strptime(str(stamp), "%Y-%m-%d").date()
    except ValueError:
        return f'pricing.last_verified "{stamp}" is not a YYYY-MM-DD date'
    age = (datetime.now(timezone.utc).date() - verified).days
    if age > max_age_days:
        return (f"pricing table last verified {age} days ago (> {max_age_days}) — "
                "provider prices move; re-check and bump pricing.last_verified")
    return None


def cmd_doctor(args) -> int:
    problems = []
    ok = lambda msg: print(f"  ok  {msg}")
    bad = lambda msg: (problems.append(msg), print(f"ERROR {msg}"))

    try:
        config.load(force_reload=True)
        ok(f"eval_config.yaml parses ({len(config.suites())} suites)")
        config.agents()
        ok("agents block valid (judge != generator)")
        prod = (config.get().get("project") or {}).get("production_model")
        gen_model = config.agents()["generation"]["model"]
        if prod and gen_model != prod:
            print(f" WARN generation model {gen_model} != production_model {prod} — "
                  "you are evaluating a model you don't ship")
    except config.ConfigError as e:
        bad(str(e))

    pkg = json.loads((config.ROOT / "package.json").read_text())
    pin = (pkg.get("devDependencies") or {}).get("promptfoo", "")
    if pin.startswith(("^", "~")):
        bad(f'promptfoo is range-pinned ("{pin}") — the engine expects an exact pin')
    installed_pkg = config.ROOT / "node_modules" / "promptfoo" / "package.json"
    if not installed_pkg.exists():
        bad("promptfoo not installed — run: npm ci")
    else:
        installed = json.loads(installed_pkg.read_text())["version"]
        if pin and installed != pin.lstrip("^~"):
            bad(f"installed promptfoo {installed} != declared {pin} — run npm ci")
        else:
            ok(f"promptfoo {installed} (exact pin)")
    try:
        import anthropic
        ok(f"anthropic SDK {anthropic.__version__}")
    except ImportError:
        bad("anthropic python package missing — run: uv sync")

    node_floor = ((pkg.get("engines") or {}).get("node") or "").lstrip(">=")
    try:
        node_version = subprocess.run(
            ["node", "--version"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        node_version = ""
    if not node_version:
        bad("node not found on PATH — promptfoo cannot run")
    elif node_floor:
        running = tuple(int(p) for p in node_version.lstrip("v").split(".")[:2])
        floor = tuple(int(p) for p in node_floor.split(".")[:2])
        if running < floor:
            bad(f"node {node_version} < required {node_floor} (engines in "
                "package.json) — promptfoo will crash mid-run; use the .nvmrc version")
        else:
            ok(f"node {node_version} (>= {node_floor})")

    if os.name != "posix":
        print(" WARN non-POSIX platform — live runs use process groups and POSIX "
              "locks; native Windows is unsupported (use WSL2)")

    config.load_env_file()
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        bad("ANTHROPIC_API_KEY missing (.env or environment) — live runs will fail")
    elif key == "YOUR_API_KEY":
        bad("ANTHROPIC_API_KEY is still the .env.example placeholder — edit .env "
            "with your real key")
    else:
        ok("ANTHROPIC_API_KEY present (api transport)")
        if not key.startswith("sk-"):
            print(" WARN ANTHROPIC_API_KEY does not look like an Anthropic key "
                  "(expected sk-… shape) — the first live call may 401")

    judge_problems = _judge_provider_problems()
    for p in judge_problems:
        bad(p)
    try:
        if not judge_problems and config.agents()["judge"]["model"].startswith("openai:"):
            ok("openai judge ready (SDK + OPENAI_API_KEY)")
    except config.ConfigError:
        pass

    preflight_id = (state.load().get("preflight") or {}).get("judge_api_model")
    if preflight_id:
        for s in config.suites():
            calibrated = (state.suite_state(s["id"]).get("calibrated") or {}).get("api_model")
            if calibrated and calibrated != preflight_id:
                print(f" WARN suite {s['id']}: judge calibrated on provider model "
                      f"{calibrated}, but preflight resolved {preflight_id} — the "
                      "provider moved the model behind its name; recalibrate")

    try:
        raw_agents = config.get().get("agents") or {}
        dead = [f"agents.{role}.max_budget_usd"
                for role in ("judge", "dataset")
                if (raw_agents.get(role) or {}).get("max_budget_usd") is not None]
        if dead:
            verb = "are" if len(dead) > 1 else "is"
            print(f" WARN {' and '.join(dead)} {verb} read by no code — per-role "
                  "ceilings live in governance.max_cost_usd (only agents.generation."
                  "max_budget_usd is live, via the provider)")
    except config.ConfigError:
        pass

    try:
        from src.science import spot_check as sc
        warning = sc.agreement_warning()
        if warning:
            print(f" WARN {warning}")
    except config.ConfigError:
        pass

    try:
        staleness = _pricing_staleness_warning()
        if staleness:
            print(f" WARN {staleness}")
    except config.ConfigError:
        pass

    print(f" info {_spend_totals_line().strip()}")
    if _git(["status", "--porcelain"]):
        print(" WARN working tree is dirty — campaigns should run clean (--require-clean)")
    print(f"\ndoctor: {'FAILED' if problems else 'all green'}")
    return 1 if problems else 0


# --------------------------------------------------------------- live runs —
PF_CONFIGS = {
    "baseline": ("promptfooconfig.yaml", "generate_tests"),
    "graded": ("promptfooconfig.graded.yaml", "generate_graded_tests"),
    "compare": ("promptfooconfig.compare.yaml", "generate_compare_graded_tests"),
    "confirm": ("promptfooconfig.compare.yaml", "generate_compare_graded_tests"),
}


def _materialize_http_config(provider: dict, mode: str, run_dir: Path) -> Path:
    """Per-run promptfoo config for an http-provider suite. promptfoo honors
    provider dicts at CONFIG level only (test-level `provider:` from a Python
    generator is silently ignored — see pf_tests.py), so the endpoint must be
    written into a config file; a per-run one keeps the checked-in configs
    agent-sdk-only. file:// refs are absolute because promptfoo resolves them
    against this file's directory, not the repo root."""
    import yaml as _yaml
    prompt_fn = "compare_prompt" if mode in ("compare", "confirm") else "current_prompt"
    generator = PF_CONFIGS[mode][1]
    provider_cfg = {k: v for k, v in provider.items() if k not in ("type", "url")}
    doc = {
        "description": f"Materialized per-run http-provider config ({mode}) — "
                       "generated by runner.py, never edited or reused",
        "prompts": [{"id": f"file://{config.ROOT}/src/loaders/pf_prompts.py:{prompt_fn}",
                     "label": "compare" if prompt_fn == "compare_prompt" else "current"}],
        "providers": [{"id": provider["url"], "label": "http",
                       **({"config": provider_cfg} if provider_cfg else {})}],
        "tests": f"file://{config.ROOT}/src/loaders/pf_tests.py:{generator}",
        "extensions": [f"file://{config.ROOT}/src/hooks.py:extension_hook"],
    }
    out = run_dir / "promptfooconfig.http.yaml"
    fsatomic.write_text_atomic(out, _yaml.safe_dump(doc, sort_keys=False))
    return out


def _plan_counts(mode: str, cases: list[dict], repeat_default: int) -> tuple[int, int, list]:
    """(gen_calls, judge_calls, per_case_repeat) mirroring the generator."""
    graded_cfg = config.get().get("graded", {})
    variants = 2 if mode in ("compare", "confirm") else 1
    if mode == "baseline":
        repeat = repeat_default
        gen = len(cases) * repeat * variants
        rubric = sum(
            1 for c in cases for a in c.get("assert") or [] if a.get("type") == "llm-rubric"
        ) * repeat * variants
        return gen, rubric, [repeat] * len(cases)
    adaptive = graded_cfg.get("adaptive_k", {})
    # Same rule as pf_tests._graded_repeat, EVAL_ADAPTIVE_K included — the
    # estimate must count the k the loader will actually run.
    screen = (set(adaptive.get("screen_suites", []))
              if adaptive.get("enabled") and os.environ.get("EVAL_ADAPTIVE_K", "1") == "1"
              else set())
    ks = [1 if c["vars"]["suite"] in screen else repeat_default for c in cases]
    gen = sum(ks) * variants
    return gen, gen, ks  # graded: one judge call per trial


def _role_gate(role: str, est_usd: float) -> str | None:
    """Optional per-role ceiling (governance.max_cost_usd.<role>)."""
    cap = cost_tracker.role_caps().get(role)
    if cap is not None and est_usd > cap:
        return (f"{role} estimate ${est_usd:.2f} exceeds "
                f"governance.max_cost_usd.{role} ${cap:.2f}")
    return None


def _window_gate(est_usd: float) -> str | None:
    """Optional rolling daily/weekly ceilings — the second line of defense:
    a run-level cap does not stop fifty capped runs."""
    gov = config.get()["governance"]
    for key, hours, label in (("max_daily_usd", 24, "24h"), ("max_weekly_usd", 168, "7d")):
        cap = gov.get(key)
        if cap is None:
            continue
        spent = cost_tracker.window_spend(hours)
        if spent + est_usd > float(cap):
            return (f"rolling {label} spend ${spent:.2f} + estimate ${est_usd:.2f} "
                    f"exceeds governance.{key} ${float(cap):.2f}")
    return None


def _dry_run_report(label: str, calls: list, basis: str,
                    total: float, ceiling: float, cache_hits: int = 0) -> int:
    """--dry-run: the full cost breakdown, printed before the lock, the run
    dir, or any call — then exit. 0 = the run would proceed at this ceiling;
    1 = it would abort. calls: [(role, n_calls, unit_usd), ...]."""
    print("─" * 72)
    print(f"DRY RUN   {label} — no calls will be made, nothing recorded")
    for role, n, unit in calls:
        print(f"  {role:<11}{n:>5} calls × ${unit:.4f}  =  ${n * unit:.2f}")
    if cache_hits:
        print(f"  cache      {cache_hits} generation replay(s) assumed free")
    print(f"  basis      {basis}")
    pct = (total / ceiling * 100) if ceiling else float("inf")
    outcome = ("within the ceiling — the run would proceed" if total <= ceiling
               else "EXCEEDS the ceiling — the run would abort (raise with --max-cost)")
    print(f"  total      ≈ ${total:.2f} vs ceiling ${ceiling:.2f} ({pct:.0f}%) — {outcome}")
    print("─" * 72)
    return 0 if total <= ceiling else 1


def _run_live(mode: str, args) -> int:
    config.load_env_file()
    suite = getattr(args, "suite", None)
    force = bool(getattr(args, "force", False))
    if suite:
        os.environ["EVAL_SUITE"] = suite

    # http-provider suites run one at a time: the endpoint is a config-level
    # promptfoo provider, so a run cannot mix it with agent-sdk suites.
    http_provider = (config.suite_by_id(suite) or {}).get("provider") if suite else None
    if not suite:
        http_suites = [s["id"] for s in config.suites() if s.get("provider")]
        if http_suites:
            print(f"ABORTED BEFORE ANY CALL — suite(s) {', '.join(http_suites)} declare "
                  "an http provider; http runs are single-suite — pass --suite",
                  file=sys.stderr)
            return 1

    # 1. state gate (before anything else — including the lock)
    forced: list[str] = []
    if mode in ("graded", "baseline"):
        target = "graded" if mode == "graded" else "baseline"
    else:
        target = {"compare": "compare", "confirm": "confirm"}[mode]
    gated_suites = [suite] if suite else [s["id"] for s in config.suites()]
    for sid in gated_suites:
        try:
            forced += state.require(sid, target, force=force)
        except state.StateError as e:
            print(f"ABORTED BEFORE ANY CALL — {e}", file=sys.stderr)
            return 1

    # Subset-first: a full graded run needs a successful small run first
    # (the "don't start with all 1,000 cases" lesson, in code not memory).
    if mode == "graded" and not getattr(args, "filter_first_n", None):
        smoke_problems = [p for sid in gated_suites for p in state.check_smoked(sid)]
        if smoke_problems and not force:
            print("ABORTED BEFORE ANY CALL — subset-first gate:\n  "
                  + "\n  ".join(smoke_problems)
                  + "\nRun the smoke first, or bypass once with --force (recorded).",
                  file=sys.stderr)
            return 1
        forced += smoke_problems

    if getattr(args, "require_clean", False) and _git(["status", "--porcelain"]):
        print("ABORTED — working tree is dirty and --require-clean is set", file=sys.stderr)
        return 1

    # 2. load cases + plan
    graded_mode = mode != "baseline"
    if mode == "confirm":
        os.environ["EVAL_HOLDOUT"] = "only"
    cases = (dataset_loader.load_graded_cases(suite) if graded_mode
             else dataset_loader.load_cases(suite))
    if not cases:
        print("no cases matched — nothing to run", file=sys.stderr)
        return 1
    agents = config.agents()
    repeat_default = int(args.repeat or (config.get().get("graded", {}).get("repeat", 3)
                                         if graded_mode else agents["generation"].get("repeat", 1)))
    os.environ["EVAL_REPEAT"] = str(repeat_default)
    concurrency = int(getattr(args, "jobs", 0) or agents["generation"].get("max_concurrency", 2))
    gen_calls, judge_calls, _ = _plan_counts(mode, cases, repeat_default)

    # 3. measured, cache-aware estimate vs the ceiling — BEFORE the first call
    if http_provider:
        # The endpoint bills whoever owns it, invisibly to this ledger — the
        # honest estimate prices generation at $0 and SAYS SO, loudly.
        estimate = cost_tracker.estimate_run_usd(0, judge_calls, suite)
        estimate["gen_unit"] = 0.0
        print("⚠ HTTP PROVIDER — generation runs on an external endpoint the engine "
              "cannot meter; the cost ceiling governs JUDGE spend only. Generation "
              "rows are ledgered as cost_unknown ($0).")
    else:
        estimate = cost_tracker.estimate_run_usd(gen_calls, judge_calls, suite)
    ceiling = float(getattr(args, "max_cost", None) or config.get()["governance"]["max_run_cost_usd"])
    ceiling_overridden = getattr(args, "max_cost", None) is not None
    suite_cap = None
    if suite and not ceiling_overridden:
        declared = (config.suite_by_id(suite) or {}).get("max_run_cost_usd")
        if declared is not None:
            suite_cap = float(declared)
            ceiling = min(ceiling, suite_cap)
    if getattr(args, "dry_run", False):
        return _dry_run_report(
            mode + (f" (suite {suite})" if suite else ""),
            [("generation", gen_calls, estimate["gen_unit"]),
             ("judge", judge_calls, estimate["judge_unit"])],
            estimate["basis"], estimate["usd"], ceiling,
            estimate["cache_hits_assumed"])
    if estimate["usd"] > ceiling and not force:
        print(
            f"ABORTED BEFORE ANY CALL — estimated cost ${estimate['usd']:.2f} "
            f"(basis: {estimate['basis']}) exceeds the ceiling ${ceiling:.2f}"
            f"{' (suite ceiling)' if suite_cap is not None and ceiling == suite_cap else ''}.\n"
            f"Reduce cells/repeat, or raise it explicitly with --max-cost.", file=sys.stderr)
        cost_tracker.record_blocked(mode, suite, estimate["usd"], ceiling,
                                    "run-ceiling", estimate["basis"])
        return 1
    gate_problem = (_role_gate("generation", gen_calls * estimate["gen_unit"])
                    or _role_gate("judge", judge_calls * estimate["judge_unit"])
                    or _window_gate(estimate["usd"]))
    if gate_problem and not force:
        print(f"ABORTED BEFORE ANY CALL — {gate_problem}", file=sys.stderr)
        cost_tracker.record_blocked(mode, suite, estimate["usd"], ceiling,
                                    gate_problem, estimate["basis"])
        return 1

    # 4. lock + run dir + crash-proof manifest
    manifest = _build_manifest(mode, suite, cases, repeat_default, concurrency, forced)
    if http_provider:
        manifest["transport"] = "http"
        # Header VALUES never land in a durable artifact (they carry keys);
        # the key names alone document the wiring.
        manifest["httpProvider"] = {
            **{k: v for k, v in http_provider.items() if k != "headers"},
            **({"headerKeys": sorted(http_provider["headers"])}
               if isinstance(http_provider.get("headers"), dict) else {}),
        }
    if ceiling_overridden:
        manifest["maxCostOverride"] = ceiling
    manifest["ceilings"] = {"run": ceiling, "suite": suite_cap,
                            "roles": cost_tracker.role_caps() or None}
    try:
        release = run_lock.acquire(manifest["runId"])
    except run_lock.LockHeld as e:
        print(f"ABORTED BEFORE ANY CALL — {e}", file=sys.stderr)
        return 1
    run_dir = config.outputs_dir() / manifest["runId"]
    _write_manifest(run_dir, manifest)
    # Same-process spending after the eval (e.g. preflight's judge smoke) must
    # land on this run's ledger too — no unmetered call paths (incident #14).
    os.environ["EVAL_RUN_DIR"] = str(run_dir)
    if http_provider:
        cost_tracker.record("alert", "http", usd=0.0,
                            note="http provider: generation unmetered — "
                                 "ceiling governs judge spend only")

    print("─" * 72)
    print(f"run       {manifest['runId']}" + (f"  (suite {suite})" if suite else ""))
    print(f"models    generation {manifest['models']['generation']}  ·  judge {manifest['models']['judge']}")
    print(f"plan      {len(cases)} cases · repeat {repeat_default}"
          + (" · 2 prompt variants" if mode in ("compare", "confirm") else "")
          + f"  →  {gen_calls} generation + {judge_calls} judge calls  ·  -j {concurrency}")
    print(f"cost      est. ≈ ${estimate['usd']:.2f} ({estimate['basis']})  ·  ceiling ${ceiling:.2f}")
    print(f"git       {str(manifest['gitSha'])[:12]}{' (dirty)' if manifest['gitDirty'] else ''}"
          f"  ·  promptfoo {manifest['promptfooVersion']}  ·  engine {manifest['engineVersion']}")
    if forced:
        print(f"forced    ⚠ {len(forced)} gate problem(s) bypassed with --force (recorded)")
    print("─" * 72)

    status.emit(run_dir, "run_banner", runId=manifest["runId"], mode=mode)

    # 5. spawn promptfoo under the watchdog
    pf_config_path = (_materialize_http_config(http_provider, mode, run_dir)
                      if http_provider else config.ROOT / PF_CONFIGS[mode][0])
    out_file = run_dir / "results.json"
    pf_args = [_promptfoo_bin(), "eval", "-c", str(pf_config_path),
               "--no-progress-bar", "-j", str(concurrency), "-o", str(out_file)]
    if getattr(args, "resume", None):
        pf_args += ["--resume"] if args.resume is True else ["--resume", args.resume]
    if getattr(args, "filter_first_n", None):
        pf_args += ["--filter-first-n", str(args.filter_first_n)]
    env = _base_env(run_dir, EVAL_SUITE=suite, EVAL_REPEAT=str(repeat_default),
                    EVAL_HTTP_PROVIDER="1" if http_provider else None)
    breaker = config.get()["governance"].get("failure_breaker", {})
    outcome = proc.run_promptfoo(pf_args, env, run_dir, ceiling, breaker,
                                 role_caps=cost_tracker.role_caps(),
                                 planned_cells=gen_calls)
    release()

    # 6. honest post-run accounting (manifest updated even for crashes)
    spend = cost_tracker.summary_for_run(run_dir)
    manifest["finishedAt"] = datetime.now(timezone.utc).isoformat()
    manifest["spend"] = spend
    manifest["killedReason"] = outcome.killed_reason
    _write_manifest(run_dir, manifest)
    cost_tracker.append_rollup(run_dir, manifest["runId"], mode, suite)
    summary.write_run_json(run_dir, manifest)

    if spend["calls"]:
        parts = "  ·  ".join(f"{k} {v['calls']} calls ${v['usd']:.2f}" for k, v in spend["byKind"].items()
                             if k in ("generation", "judge"))
        print(f"\nspend     ${spend['totalUsd']:.2f} measured  ({parts})")
    cache = spend["byKind"].get("cache_hit") or {}
    saved = sum((e.get("saved_usd") or 0) for e in cost_tracker.read_ledger(run_dir)
                if e.get("kind") == "cache_hit")
    paid_calls = sum(v["calls"] for k, v in spend["byKind"].items() if k in ("generation", "judge"))
    print(f"receipt   ${spend['totalUsd']:.2f} spent · {paid_calls} paid calls · "
          f"{cache.get('calls', 0)} cached (≈${saved:.2f} avoided) · "
          f"ceiling ${ceiling:.2f} ({(spend['totalUsd'] / ceiling * 100) if ceiling else 0:.0f}% used)")

    if outcome.killed_reason or not out_file.exists():
        why = outcome.killed_reason or f"promptfoo produced no results (exit {outcome.returncode})"
        status.emit(run_dir, "run_end", ok=False, note=why)
        summary.write_report(run_dir, manifest)
        print(f"\nRUN FAILED — {why}", file=sys.stderr)
        return 1

    rows = parse_results.rows_of(out_file)
    if not rows:
        status.emit(run_dir, "run_end", ok=False, note="zero result rows")
        print("\nRUN FAILED — zero result rows", file=sys.stderr)
        return 1

    failures = sum(1 for r in rows if r.get("success") is not True)
    status.emit(run_dir, "run_end", ok=True,
                note=f"{len(rows)} rows, {failures} with findings")
    report = summary.write_report(run_dir, manifest)
    if graded_mode and not getattr(args, "no_stage_record", False):
        summary.archive_graded(run_dir, manifest)
        _check_judge_api_drift(run_dir, gated_suites)
    regression_failed = sorted({
        str(v.get("cell"))
        for r in rows
        for v in [((r.get("testCase") or {}).get("vars") or {})]
        if v.get("regression") == "true" and r.get("success") is not True})
    if regression_failed:
        # A pinned case failing outranks any aggregate score: this exact
        # failure was fixed once already.
        print(f"\nREGRESSION FAILED — pinned case(s) failing again: "
              f"{', '.join(regression_failed)}.\nThe run fails regardless of the "
              f"aggregate score; nothing is recorded. Report: {config.display_path(report)}",
              file=sys.stderr)
        return 1
    confirm_failed = ([] if getattr(args, "no_stage_record", False)
                      else _record_run_stages(mode, suite, manifest, rows,
                                              getattr(args, "filter_first_n", None)))
    print(f"\n{'COMPLETED WITH FINDINGS' if failures else 'COMPLETED CLEAN'} — "
          f"{len(rows) - failures}/{len(rows)} case-rows green  ·  report: {config.display_path(report)}")
    if mode == "compare":
        print(f"next: prompt-eval verdict {config.display_path(out_file)} --target <dimension | contract:fn>")
    if confirm_failed:
        print(f"\nCONFIRM FAILED — promotion did not hold on holdout for: "
              f"{', '.join(confirm_failed)}.\nRevert (copy the prior production prompt "
              f"back) and re-run compare; a promotion that fails confirmation is "
              f"reverted, not argued with.", file=sys.stderr)
        return 1
    if mode == "confirm" and not getattr(args, "filter_first_n", None):
        print("CONFIRMED — promotion holds on holdout")
    return 0


def _record_run_stages(mode: str, suite: str | None, manifest: dict, rows: list,
                       filter_first_n) -> list[str]:
    """Record the pipeline stages a finished live run has earned.

    Returns the suites whose confirm holdout had findings — nothing is
    recorded for those; the caller reverts the promotion instead."""
    if mode == "graded":
        if filter_first_n:
            # A subset run proves the plumbing, not the baseline.
            for sid in ([suite] if suite else [s["id"] for s in config.suites()]):
                state.record_smoked(sid, manifest["runId"])
        else:
            baselined = [suite] if suite else [s["id"] for s in config.suites() if s.get("rubric")]
            for sid in baselined:
                state.record(sid, "baselined", {"runId": manifest["runId"]})
        return []
    if mode != "confirm" or filter_first_n:
        return []
    gated = [suite] if suite else [s["id"] for s in config.suites()]
    failed = []
    for sid in gated:
        srows = [r for r in rows
                 if (r.get("testCase") or {}).get("vars", {}).get("suite") == sid]
        if not srows:
            continue
        if any(r.get("success") is not True for r in srows):
            failed.append(sid)
        else:
            state.record(sid, "confirmed", {"runId": manifest["runId"], "rows": len(srows)})
    return failed


# -------------------------------------------------------------- preflight —
def cmd_preflight(args) -> int:
    """One real generation case (configured model + caps through the native
    provider path) + one judge call. The cheapest possible proof that the
    REAL configuration works (incident #9)."""
    config.load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY missing — put it in .env first", file=sys.stderr)
        return 1
    judge_problems = _judge_provider_problems()
    if judge_problems:
        for p in judge_problems:
            print(p, file=sys.stderr)
        return 1
    suite = args.suite
    if not suite:
        if not config.suites():
            print("no suites configured — drop a prompt into input/ and run "
                  "`prompt-eval init` first", file=sys.stderr)
            return 1
        suite = config.suites()[0]["id"]
    ns = argparse.Namespace(
        suite=suite, force=args.force, repeat=1,
        jobs=1, max_cost=args.max_cost or 0.30, resume=None, filter_first_n=1,
        require_clean=False)
    # Preflight is itself the thing the gates require — bypass them, then a
    # judge smoke, then record the preflight fact.
    ns.force = True
    rc = _run_live("baseline", ns)
    if rc != 0:
        return rc
    from src.evaluators.llm_judge import judge_call
    reply = judge_call('Reply with exactly the word OK and nothing else.')
    if "OK" not in reply:
        print(f"judge smoke reply unexpected: {reply[:80]}", file=sys.stderr)
        return 1
    print("judge smoke: OK")
    # The smoke's ledger entry carries the provider-resolved judge model id —
    # the free canary `doctor` compares against each suite's calibration.
    state.record_preflight({**state.preflight_fingerprint(),
                            "judge_api_model": _judge_api_model_of_run()})
    print("PREFLIGHT GREEN — real configuration proven; campaigns unlocked.")
    return 0


def _judge_api_model_of_run(run_dir: Path | None = None) -> str | None:
    rdir = run_dir or cost_tracker.run_dir()
    if rdir is None:
        return None
    ids = sorted({e.get("apiModel") for e in cost_tracker.read_ledger(rdir)
                  if e.get("kind") == "judge" and e.get("apiModel")})
    return ";".join(ids) if ids else None


def _check_judge_api_drift(run_dir: Path, suite_ids: list) -> None:
    """Post-run verify (the id is only knowable after a call): the judge's
    provider-resolved model id must match what each suite was calibrated on.
    A mismatch warns now and gates the NEXT run via state (tightening)."""
    resolved = _judge_api_model_of_run(run_dir)
    if not resolved:
        return
    for sid in suite_ids:
        calibrated = (state.suite_state(sid).get("calibrated") or {}).get("api_model")
        if calibrated and calibrated != resolved:
            print(f"WARN judge resolved to provider model {resolved} but suite {sid} "
                  f"was calibrated on {calibrated} — the provider moved the model; "
                  "recalibrate before the next graded run", file=sys.stderr)
            cost_tracker.record("alert", config.agents()["judge"]["model"], usd=0.0,
                                note=f"judge api-model drift: calibrated {calibrated}, got {resolved}")
            state.flag_api_model_drift(sid, resolved)


# ------------------------------------------------------------ subcommands —
def cmd_matrix(args) -> int:
    from src.science.gen_matrix import generate
    specs_dir = config.specs_dir()
    specs = sorted(specs_dir.glob("*.yaml"))
    if args.suite:
        specs = [s for s in specs if s.stem == args.suite]
    if not specs:
        print("no matrix specs matched", file=sys.stderr)
        return 1
    for spec in specs:
        r = generate(spec)
        print(f"  {r['suite']}: {r['cells']} cells → {config.display_path(r['out'])} "
              f"({r['holdout']} holdout)" + (f"  note: {r['note']}" if r["note"] else ""))
    return 0


def calibration_judge_calls(suites: list[dict]) -> int:
    """Exact call count for a calibration run: the same golden files
    calibrate_suite() iterates (the suite's declared goldens, not a global
    average — a 4-golden suite once estimated at the 38-file fleet mean)."""
    from src.science.calibrate import _goldens_of_suite
    from src.providers.mock_golden import golden_files

    cal = config.get().get("graded", {}).get("calibration", {})
    samples_n = int(os.environ.get("EVAL_CAL_SAMPLES") or cal.get("samples", 3))
    n_files = sum(
        len(golden_files(config.fixtures_dir() / "golden" / g, golden_set))
        for suite in suites
        for g in _goldens_of_suite(suite)
        for golden_set in ("pass", "fail", "mid")
    )
    return samples_n * max(1, n_files)


def cmd_calibrate(args) -> int:
    from src.science.calibrate import calibrate_suite
    config.load_env_file()
    suites = [s for s in config.suites() if s.get("rubric")]
    if args.suite:
        suites = [s for s in suites if s["id"] == args.suite]
    if not suites:
        print("no suites with a rubric: entry to calibrate", file=sys.stderr)
        return 1
    for sid in [s["id"] for s in suites]:
        try:
            state.require(sid, "calibrate", force=args.force)
        except state.StateError as e:
            print(f"ABORTED — {e}", file=sys.stderr)
            return 1

    if not os.environ.get("MOCK_GRADED_JUDGE"):
        # Placeholder goldens gate (paid path only — a mock band calibration on a
        # scaffold is a legitimate free wiring check): a judge calibrated against
        # EVAL-INIT-PLACEHOLDER fixtures would earn a green record proving nothing.
        for s in suites:
            placeholders = checks.placeholder_files(s)
            if placeholders:
                if args.force:
                    print(f"WARN  forced past placeholder content in {s['id']} "
                          f"({len(placeholders)} file(s)) — this calibration proves "
                          "nothing about your real goldens")
                else:
                    print(f"ABORTED BEFORE ANY CALL — suite {s['id']} still has "
                          f"EVAL-INIT-PLACEHOLDER content in {len(placeholders)} "
                          "file(s); a judge calibrated on placeholders proves "
                          "nothing. Replace them (`validate` lists which), or "
                          "--force to record a bypass.", file=sys.stderr)
                    return 1
        judge_calls = calibration_judge_calls(suites)
        rate = cost_tracker.measured_calibration_judge_rate()
        judge_unit = rate["judge_usd_per_call"] if rate else \
            config.get()["governance"].get("estimate", {}).get("judge_usd", 0.2)
        est = {"usd": judge_calls * judge_unit,
               "basis": rate["source"] if rate else "config fallback (no calibrate ledger)"}
        ceiling = float(args.max_cost or config.get()["governance"]["max_run_cost_usd"])
        if getattr(args, "dry_run", False):
            return _dry_run_report(
                "calibrate (" + ", ".join(s["id"] for s in suites) + ")",
                [("judge", judge_calls, judge_unit)], est["basis"], est["usd"], ceiling)
        if est["usd"] > ceiling and not args.force:
            print(f"ABORTED — calibration estimate ${est['usd']:.2f} ({judge_calls} calls, "
                  f"basis {est['basis']}) exceeds ceiling ${ceiling:.2f} "
                  "(raise with --max-cost)", file=sys.stderr)
            cost_tracker.record_blocked("calibrate", args.suite, est["usd"], ceiling,
                                        "run-ceiling", est["basis"])
            return 1
        gate_problem = _role_gate("judge", est["usd"]) or _window_gate(est["usd"])
        if gate_problem and not args.force:
            print(f"ABORTED — {gate_problem}", file=sys.stderr)
            cost_tracker.record_blocked("calibrate", args.suite, est["usd"], ceiling,
                                        gate_problem, est["basis"])
            return 1
    elif getattr(args, "dry_run", False):
        print("DRY RUN — mock judge active: $0.00, nothing to spend")
        return 0

    run_id = f"calibrate-{_stamp()}"
    try:
        release = run_lock.acquire(run_id)
    except run_lock.LockHeld as e:
        print(f"ABORTED — {e}", file=sys.stderr)
        return 1
    run_dir = config.outputs_dir() / run_id
    os.environ["EVAL_RUN_DIR"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    green = True
    for suite in suites:
        record = calibrate_suite(suite)
        green = green and record["green"]
    release()
    spend = cost_tracker.summary_for_run(run_dir)
    cost_tracker.append_rollup(run_dir, run_id, "calibrate", args.suite)
    if spend["calls"]:
        print(f"\nspend     ${spend['totalUsd']:.2f} measured ({spend['calls']} judge calls)")
    if green:
        print("\nCALIBRATION GREEN — every golden sample landed in band.")
        return 0
    print("\nCALIBRATION FAILED — fix rubric anchors, not thresholds; then re-run.")
    return 1


def latest_compare_results() -> Path | None:
    """Newest compare/confirm results file, by run-dir timestamp."""
    candidates = [p for pat in ("compare-*", "confirm-*")
                  for p in config.outputs_dir().glob(f"{pat}/results.json")]
    return max(candidates, key=lambda p: p.parent.name.split("-", 1)[1]) if candidates else None


def cmd_verdict(args) -> int:
    from src.science.pairwise import _collect_cells, compare_verdict
    config.load_env_file()
    if args.results:
        results_path = Path(args.results)
    else:
        results_path = latest_compare_results()
        if results_path is None:
            print("VERDICT REFUSED — no results file given and no compare/confirm "
                  "run found in outputs", file=sys.stderr)
            return 1
        print(f"results   {config.display_path(results_path)} (latest; pass a path to override)")

    # Every spending entrypoint estimates and gates BEFORE the first call
    # (incident #14) — verdict spends 2 blinded judge calls per cell (both
    # orders; invalid-JSON re-asks are rare and bounded, not multiplied in).
    if not os.environ.get("MOCK_PAIRWISE"):
        cells = _collect_cells(parse_results.rows_of(results_path))
        if cells:  # empty → compare_verdict raises its own error below
            est = cost_tracker.estimate_run_usd(0, len(cells) * 2, cells[0]["suite"])
            ceiling = float(args.max_cost or config.get()["governance"]["max_run_cost_usd"])
            if getattr(args, "dry_run", False):
                return _dry_run_report(
                    f"verdict ({len(cells)} cells, both blinded orders)",
                    [("judge", len(cells) * 2, est["judge_unit"])],
                    est["basis"], est["usd"], ceiling)
            if est["usd"] > ceiling and not args.force:
                print(f"ABORTED BEFORE ANY CALL — verdict estimate ${est['usd']:.2f} "
                      f"({len(cells) * 2} judge calls, basis {est['basis']}) exceeds "
                      f"ceiling ${ceiling:.2f} (raise with --max-cost)", file=sys.stderr)
                cost_tracker.record_blocked("verdict", cells[0]["suite"], est["usd"],
                                            ceiling, "run-ceiling", est["basis"])
                return 1
            gate_problem = _role_gate("judge", est["usd"]) or _window_gate(est["usd"])
            if gate_problem and not args.force:
                print(f"ABORTED BEFORE ANY CALL — {gate_problem}", file=sys.stderr)
                cost_tracker.record_blocked("verdict", cells[0]["suite"], est["usd"],
                                            ceiling, gate_problem, est["basis"])
                return 1
    elif getattr(args, "dry_run", False):
        print("DRY RUN — mock pairwise judge active: $0.00, nothing to spend")
        return 0

    run_id = f"verdict-{_stamp()}"
    try:
        release = run_lock.acquire(run_id)
    except run_lock.LockHeld as e:
        print(f"ABORTED — {e}", file=sys.stderr)
        return 1
    run_dir = config.outputs_dir() / run_id
    os.environ["EVAL_RUN_DIR"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        record = compare_verdict(results_path, args.target,
                                 reason=args.reason, new_evidence=args.new_evidence)
    except (ValueError, PermissionError) as e:
        print(f"VERDICT REFUSED — {e}", file=sys.stderr)
        release()
        return 1
    release()
    cost_tracker.append_rollup(run_dir, run_id, "verdict", None)
    _check_judge_api_drift(run_dir, [record["suite"]])
    # 0 = PROMOTE, 1 = REJECT, 2 = INSUFFICIENT_EVIDENCE
    return {"PROMOTE": 0, "REJECT": 1, "INSUFFICIENT_EVIDENCE": 2}.get(
        record.get("verdict", "REJECT"), 1)


def cmd_promote(args) -> int:
    compared = state.suite_state(args.suite).get("compared") or {}
    if compared.get("verdict") != "PROMOTE" and not args.force:
        print(f'ABORTED — suite {args.suite} has no PROMOTE verdict on record '
              f'(found: {compared.get("verdict")!r}). Run compare + verdict first.', file=sys.stderr)
        return 1
    suite = config.suite_by_id(args.suite)
    cases = dataset_loader.load_cases(args.suite)
    prompt_file = cases[0]["vars"]["promptFile"]
    if str(prompt_file).startswith("file://"):
        print(f"ABORTED — this suite's prompt source is a python constant "
              f"({prompt_file}); promote copies prompt FILES. Edit the constant "
              "at its source instead.", file=sys.stderr)
        return 1
    src_path = config.resolve(config.get()["prompts"].get("candidates_dir", "prompts/candidates")) / prompt_file
    dst_path = config.resolve(config.get()["prompts"]["production_dir"]) / prompt_file
    if not src_path.exists():
        print(f"ABORTED — no candidate at {src_path}", file=sys.stderr)
        return 1
    shutil.copy2(src_path, dst_path)
    state.record(args.suite, "promoted", {
        "promptFile": prompt_file,
        "sha": state.sha256_file(dst_path),
        "compareRecord": compared.get("record"),
    })
    print(f"PROMOTED {prompt_file} (candidate → production).")
    print(f"next: prompt-eval confirm --suite {args.suite}   # holdout cells, then commit both")
    return 0


def _next_action(suite_id: str, stage: str, smoke_pending: bool) -> tuple[list[str], str]:
    """(argv for runner.main, display note) for the next required stage.
    The argv is exactly the printed command — `round --run` can execute only
    what `round` displays, so no stage can be combined or skipped."""
    table = {
        "validated": (["validate"], ""),
        "calibrated": (["calibrate", "--suite", suite_id], ""),
        "baselined": ((["graded", "--suite", suite_id, "--filter-first-n", "2"], "(smoke first)")
                      if smoke_pending else (["graded", "--suite", suite_id], "")),
        "compared": (["compare", "--suite", suite_id], "(needs a candidate prompt)"),
        "promoted": (["promote", "--suite", suite_id], "(only on a PROMOTE verdict)"),
        "confirmed": (["confirm", "--suite", suite_id], ""),
    }
    return table[stage]


def cmd_round(args) -> int:
    """Pipeline status + the next required action per suite. With --run,
    execute that one next command after confirmation (nothing else)."""
    if not config.suites():
        print("No suites declared yet. Drop a prompt (or a folder) into input/ and run "
              "`prompt-eval init` — it detects what you dropped, scaffolds the rest, "
              "and wires the suite (input/README.md has the full menu). Or take the "
              "guided tour on real content: prompt-eval init --example support-reply")
        return 0
    from src.dashboard.data import pipeline_view
    next_argv: list[str] | None = None
    view = {s["suite"]: s for s in (pipeline_view().get("suites") or [])}
    for suite in config.suites():
        if args.suite and suite["id"] != args.suite:
            continue
        sv = view.get(suite["id"]) or {}
        stages = sv.get("stages") or []
        # A stage recorded under a different fingerprint (e.g. a mock-band
        # calibration vs the configured judge) is shown STALE, and `next`
        # points at re-earning it — the same view `why` and the dashboard use.
        done = [x["stage"] + (" (STALE)" if x["stale"] else "")
                for x in stages if x["done"]]
        nxt = sv.get("next")
        smoke_pending = bool(sv.get("smokeProblems"))
        placeholders = sv.get("placeholders") or []
        print(f"{suite['id']:<28} {' → '.join(done) or '(nothing recorded)'}")
        if placeholders:
            print(f"{'':<28} scaffolded (placeholders remain in {len(placeholders)} "
                  f"file(s) — replace them, `validate` lists which)")
        if nxt:
            argv, note = _next_action(suite["id"], nxt, smoke_pending)
            print(f"{'':<28} next: prompt-eval {' '.join(argv)}" + (f"  {note}" if note else ""))
            if args.suite:
                next_argv = argv
    if not state.load().get("preflight"):
        print("\npreflight: NOT RUN — required before any campaign (prompt-eval preflight)")
    from src.science import spot_check as sc
    warning = sc.agreement_warning()
    if warning:
        print(f"\nWARN {warning}")
    print(f"\n{_spend_totals_line()}")

    if getattr(args, "run", False):
        if not args.suite:
            print("\nround --run needs --suite (one next command at a time)", file=sys.stderr)
            return 1
        if next_argv is None:
            print("\nnothing to run — the pipeline is complete for this suite")
            return 0
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                print("\nround --run needs a TTY to confirm (or pass --yes)", file=sys.stderr)
                return 1
            reply = input(f"\nrun `prompt-eval {' '.join(next_argv)}` now? [y/N] ").strip().lower()
            if reply != "y":
                print("not run")
                return 0
        print(f"\n→ prompt-eval {' '.join(next_argv)}")
        return main(next_argv)
    return 0


def cmd_why(args) -> int:
    """Explain the pipeline's current blocked state in prose: which stage,
    stale on WHICH fingerprint key, and the exact command that fixes it —
    the CLI surface of the dashboard's pipeline view."""
    from src.dashboard.data import pipeline_view
    view = pipeline_view()
    for s in view.get("suites") or []:
        if args.suite and s["suite"] != args.suite:
            continue
        print(f"\n{s['suite']}")
        for stage in s["stages"]:
            if stage["done"] and not stage["stale"]:
                mark = "done"
            elif stage["stale"]:
                mark = f"STALE on {', '.join(stage['stale'])} — re-earn it"
            else:
                mark = "not yet earned"
            extras = []
            if stage.get("green") is False:
                extras.append("NOT GREEN")
            if stage.get("restored"):
                extras.append("restored")
            if stage.get("verdict"):
                extras.append(f"verdict {stage['verdict']}")
            print(f"  {stage['stage']:<11} {mark}" + (f"  ({', '.join(extras)})" if extras else ""))
        for p in s.get("smokeProblems") or []:
            print(f"  smoke       {p}")
        if s.get("placeholders"):
            print(f"  scaffold    placeholders remain in {len(s['placeholders'])} file(s)")
        nxt = s.get("next")
        if nxt:
            smoke_pending = bool(s.get("smokeProblems"))
            argv, note = _next_action(s["suite"], nxt, smoke_pending)
            print(f"  next        prompt-eval {' '.join(argv)}" + (f"  {note}" if note else ""))
        else:
            print("  next        nothing — pipeline complete")
    for p in (view.get("preflight") or {}).get("problems") or []:
        print(f"\npreflight: {p}")
    return 0


def _spend_totals_line() -> str:
    day, week = cost_tracker.window_spend(24), cost_tracker.window_spend(168)
    gov = config.get()["governance"]
    caps = " · ".join(
        f"{label} cap ${float(gov[key]):.2f}"
        for key, label in (("max_daily_usd", "daily"), ("max_weekly_usd", "weekly"))
        if gov.get(key) is not None)
    savings = cost_tracker.savings_totals()
    lines = (f"spend     ${day:.2f} last 24h · ${week:.2f} last 7d"
             + (f"  ({caps})" if caps else ""))
    if savings["cacheHits"] or savings["blockedCount"]:
        lines += (f"\nsaved     ≈${savings['cacheSavedUsd']:.2f} across "
                  f"{savings['cacheHits']} cache replays · "
                  f"≈${savings['blockedUsd']:.2f} refused at the gates "
                  f"({savings['blockedCount']} aborts)")
    return lines


def _run_dataset_side_command(args, label: str, worker):
    """Shared scaffolding for cheap dataset-model side commands (gen-cases,
    perturb): pre-spend gates + --dry-run, run lock, ledger, rollup —
    a spending entrypoint like every other one (incident #14: unmetered
    side-paths). Returns (rc, result|None)."""
    config.load_env_file()
    if not os.environ.get("MOCK_DATASET"):
        est_cfg = config.get()["governance"].get("estimate", {})
        est = est_cfg.get("dataset_usd", 0.10)  # one call generates everything
        ceiling = float(args.max_cost or config.get()["governance"]["max_run_cost_usd"])
        if getattr(args, "dry_run", False):
            return _dry_run_report(f"{label} (suite {args.suite})",
                                   [("dataset", 1, est)],
                                   "config estimate (governance.estimate.dataset_usd)",
                                   est, ceiling), None
        if est > ceiling and not args.force:
            print(f"ABORTED — {label} estimate ${est:.2f} exceeds ceiling "
                  f"${ceiling:.2f} (raise with --max-cost)", file=sys.stderr)
            cost_tracker.record_blocked(label, args.suite, est, ceiling,
                                        "run-ceiling", "config estimate")
            return 1, None
        gate_problem = _role_gate("dataset", est) or _window_gate(est)
        if gate_problem and not args.force:
            print(f"ABORTED — {gate_problem}", file=sys.stderr)
            cost_tracker.record_blocked(label, args.suite, est, ceiling,
                                        gate_problem, "config estimate")
            return 1, None
    elif getattr(args, "dry_run", False):
        print("DRY RUN — mock dataset model active: $0.00, nothing to spend")
        return 0, None

    run_id = f"{label}-{_stamp()}"
    try:
        release = run_lock.acquire(run_id)
    except run_lock.LockHeld as e:
        print(f"ABORTED — {e}", file=sys.stderr)
        return 1, None
    run_dir = config.outputs_dir() / run_id
    os.environ["EVAL_RUN_DIR"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = worker()
    finally:
        release()
    spend = cost_tracker.summary_for_run(run_dir)
    cost_tracker.append_rollup(run_dir, run_id, label, args.suite)
    if spend["calls"]:
        print(f"spend     ${spend['totalUsd']:.2f} measured ({spend['calls']} call)")
    return 0, result


def cmd_gen_cases(args) -> int:
    """Draft realistic ordinary-user test cases with the cheap dataset model."""
    from src.science.gen_cases import generate_cases
    rc, result = _run_dataset_side_command(
        args, "gen-cases",
        lambda: generate_cases(args.suite, args.count,
                               out_dir=Path(args.out) if args.out else None))
    if rc != 0 or result is None:
        return rc
    print(f"{len(result['cases'])} draft cases ({result['model']}) → {result['out']}")
    print("review them, merge the keepers into the suite's graded spec, then: "
          f"prompt-eval matrix --suite {args.suite}")
    return 0


def cmd_perturb(args) -> int:
    """Paraphrase existing probes with the cheap dataset model — robustness
    drafts, never auto-wired."""
    from src.science.perturb import generate_perturbations

    def worker():
        return generate_perturbations(args.suite, args.variants,
                                      out_dir=Path(args.out) if args.out else None)

    try:
        rc, result = _run_dataset_side_command(args, "perturb", worker)
    except ValueError as e:
        print(f"PERTURB REFUSED — {e}", file=sys.stderr)
        return 1
    if rc != 0 or result is None:
        return rc
    n = sum(len(e["variants"]) for e in result["perturbations"])
    print(f"{n} paraphrase variants of {len(result['perturbations'])} probes "
          f"({result['model']}) → {result['out']}")
    print("review for same-intent realism, merge keepers into the graded spec, "
          f"then: prompt-eval matrix --suite {args.suite}")
    return 0


def _canary_cells(suite_id: str, n: int) -> list[str]:
    """Fixed, hash-ranked canary subset (same ranking idea as the holdout
    reservation): deterministic, cherry-pick-proof, never holdout cells."""
    import hashlib
    matrix = config.graded_dir() / f"{suite_id}.yaml"
    if not matrix.exists():
        return []
    import yaml as _yaml
    cells = [c["vars"]["cell"] for c in _yaml.safe_load(matrix.read_text()) or []
             if (c.get("vars") or {}).get("cell")
             and str(c["vars"].get("holdout")) != "true"]
    ranked = sorted(set(cells),
                    key=lambda c: hashlib.sha256(f"{suite_id}|{c}".encode()).hexdigest())
    return ranked[:n]


def _monitor_baseline_dims(suite_id: str, exclude_run: str) -> tuple[dict, str] | None:
    """Per-dimension score means from the newest archived graded record for
    this suite (the baseline distribution the canary is compared against)."""
    hist = config.history_dir()
    if not hist.is_dir():
        return None
    for path in sorted(hist.glob("graded-*.json"), reverse=True):
        if path.stem == exclude_run:
            continue
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        dims: dict[str, list[float]] = {}
        for case in rec.get("cases") or []:
            if case.get("suite") != suite_id or not isinstance(case.get("scores"), dict):
                continue
            for d, v in case["scores"].items():
                if isinstance(v, (int, float)):
                    dims.setdefault(d, []).append(v)
        if dims:
            return {d: sum(v) / len(v) for d, v in dims.items()}, path.name
    return None


def cmd_monitor(args) -> int:
    """On-demand model-drift canary: a tiny fixed graded subset compared to
    the baselined dimension means. No scheduler on purpose — run it when you
    want a check; the engine has no business owning cron."""
    config.load_env_file()
    n = int(config.get().get("graded", {}).get("monitor", {}).get("cells", 2))
    cells = _canary_cells(args.suite, n)
    if not cells:
        print(f"no canary cells for suite {args.suite} — run `prompt-eval matrix` first",
              file=sys.stderr)
        return 1
    os.environ["EVAL_MONITOR_CELLS"] = ",".join(cells)
    try:
        ns = argparse.Namespace(
            suite=args.suite, force=args.force, repeat=1, jobs=1,
            max_cost=args.max_cost or min(0.5, float(config.get()["governance"]["max_run_cost_usd"])),
            resume=None, filter_first_n=None, require_clean=False,
            dry_run=getattr(args, "dry_run", False), no_stage_record=True)
        rc = _run_live("graded", ns)
    finally:
        os.environ.pop("EVAL_MONITOR_CELLS", None)
    if rc != 0 or getattr(args, "dry_run", False):
        return rc

    run_dir = Path(os.environ["EVAL_RUN_DIR"])
    rows = parse_results.rows_of(run_dir / "results.json")
    current: dict[str, list[float]] = {}
    for r in rows:
        for c in (r.get("gradingResult") or {}).get("componentResults") or []:
            for d, v in (c.get("namedScores") or {}).items():
                if isinstance(v, (int, float)):
                    current.setdefault(d, []).append(v)
    current_means = {d: sum(v) / len(v) for d, v in current.items()}
    baseline = _monitor_baseline_dims(args.suite, run_dir.name)
    if baseline is None:
        print("monitor: no archived graded baseline to compare against — "
              "run a full graded baseline first", file=sys.stderr)
        return 1
    baseline_means, source = baseline
    threshold = float(config.get().get("graded", {}).get("promotion", {}).get("max_regression", 0.25))
    drifted = {d: (current_means[d], baseline_means[d])
               for d in current_means
               if d in baseline_means and abs(current_means[d] - baseline_means[d]) > threshold}
    stamp = _stamp()
    record = {"suite": args.suite, "runId": run_dir.name, "canaryCells": cells,
              "dims": current_means, "baselineDims": baseline_means,
              "baselineSource": source, "threshold": threshold,
              "drifted": {d: {"canary": c, "baseline": b} for d, (c, b) in drifted.items()},
              "at": datetime.now(timezone.utc).isoformat()}
    out = config.history_dir() / f"monitor-{args.suite}-{stamp}.json"
    fsatomic.write_text_atomic(out, json.dumps(record, indent=2))
    print(f"\nmonitor   canary {', '.join(cells)} vs baseline {source}")
    for d in sorted(current_means):
        base = baseline_means.get(d)
        mark = " ⚠ DRIFT" if d in drifted else ""
        print(f"  {d:<20} canary {current_means[d]:.2f} · baseline "
              + (f"{base:.2f}" if base is not None else "n/a") + mark)
    if drifted:
        print(f"\nMONITOR DRIFT — {', '.join(sorted(drifted))} moved more than "
              f"±{threshold} vs the baseline. Recalibrate and re-baseline before "
              "trusting any new verdicts. Record: " + config.display_path(out),
              file=sys.stderr)
        return 1
    print(f"monitor: no drift beyond ±{threshold} · record {config.display_path(out)}")
    return 0


def cmd_spot_check(args) -> int:
    """Human spot-check of judge verdicts on a finished run — free, offline,
    interactive. Labels persist; the rolling agreement gates trust."""
    from src.science import spot_check as sc
    run_dir = Path(args.run)
    if not sys.stdin.isatty() and not os.environ.get("EVAL_SPOT_ANSWERS"):
        print("SPOT-CHECK REFUSED — interactive labels need a TTY "
              "(the whole point is a human, not a script)", file=sys.stderr)
        return 1
    canned = list(os.environ.get("EVAL_SPOT_ANSWERS", ""))  # test hook: "ynyn..."

    def ask(dim: str, _sample: dict) -> bool:
        if canned:
            return canned.pop(0) == "y"
        while True:
            reply = input(f"  your verdict for {dim} — pass? [y/n] ").strip().lower()
            if reply in ("y", "n"):
                return reply == "y"

    try:
        result = sc.spot_check(run_dir, args.n, ask)
    except ValueError as e:
        print(f"SPOT-CHECK REFUSED — {e}", file=sys.stderr)
        return 1
    print("─" * 72)
    print(f"this session: {result['labels']} labels · agreement "
          f"{result['agreement']:.0%}" if result["agreement"] is not None else "no labels")
    for dim, rate in sorted((result.get("perDim") or {}).items()):
        print(f"  {dim}: {rate:.0%}")
    rolling = sc.rolling_agreement()
    if rolling["labels"]:
        print(f"rolling (current judge+rubric): {rolling['agreement']:.0%} "
              f"over {rolling['labels']} labels")
    warning = sc.agreement_warning()
    if warning:
        print(f"WARN {warning}", file=sys.stderr)
        return 1
    return 0


def cmd_pin(args) -> int:
    """Pin a failing cell into the permanent regression set (free)."""
    from src.science.pin import pin_cell
    try:
        result = pin_cell(Path(args.run), args.cell, note=args.note)
    except ValueError as e:
        print(f"PIN REFUSED — {e}", file=sys.stderr)
        return 1
    print(f"pinned {result['cell']} (suite {result['suite']}) → "
          f"{config.display_path(result['path'])}")
    print("it now runs in every graded/compare/confirm pass; any failure fails "
          "the run loudly. Re-run the free `validate` (the regression set is "
          "fingerprinted), then `graft` to restore unaffected stages.")
    return 0


def cmd_history(args) -> int:
    """Verify the hash-chained audit ledgers in outputs/history — a modified,
    inserted, or deleted line breaks every hash after it (see chain.py)."""
    from src.utils import chain
    hist = config.history_dir()
    ledgers = [hist / "verdicts.jsonl", cost_tracker.rollup_path(),
               cost_tracker.blocked_path(), hist / "spot-checks.jsonl"]
    problems: list[str] = []
    checked = 0
    for path in ledgers:
        if not path.exists():
            continue
        checked += 1
        probs = chain.verify_chain(path)
        problems += probs
        print(f"  {'BROKEN' if probs else 'ok    '} {config.display_path(path)}")
    verdicts = hist / "verdicts.jsonl"
    for i, e in enumerate(cost_tracker._read_jsonl(verdicts), start=1):
        sha = e.get("recordSha")
        if not sha:
            continue  # legacy entry, pre-chaining
        actual = state.sha256_file(hist / str(e.get("record")))
        if actual != sha:
            problems.append(f"verdicts.jsonl:{i}: compare record {e.get('record')} "
                            "does not match its recorded sha (edited or missing)")
    if not checked:
        print("no chained ledgers yet — nothing to verify")
        return 0
    for p in problems:
        print(f"BROKEN {p}", file=sys.stderr)
    print(f"\nhistory: {'TAMPER-EVIDENT BREAKS FOUND' if problems else 'chain intact'}")
    return 1 if problems else 0


def cmd_graft(args) -> int:
    """Restore wiped pipeline state whose fingerprints still match the backup
    snapshot — recovery after a config-only edit forced a re-validate. With
    --from-history, rebuild from outputs/history records instead (the
    disaster path when the state file and its backup are both gone)."""
    if args.snapshot:
        print(f"snapshot: {state.snapshot()}")
        return 0
    try:
        lines = state.rebuild_from_history() if args.from_history else state.graft()
    except state.StateError as e:
        print(f"GRAFT REFUSED — {e}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    if not lines:
        print("nothing to graft — no wiped stages with a matching snapshot")
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"REPORT REFUSED — no manifest.json in {run_dir} — only live runs "
              "(preflight/baseline/graded/compare/confirm) write one; validate/"
              "calibrate dirs have nothing to report", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text())
    if getattr(args, "format", "md") == "json":
        out = summary.write_run_json(run_dir, manifest)
        print(out.read_text())
        return 0
    out = summary.write_report(run_dir, manifest)
    print(f"report: {out}")
    return 0


def cmd_dashboard(args) -> int:
    """Local, read-only, 127.0.0.1-only dashboard over the run artifacts.
    Owns what the engine owns (spend, governance, pipeline state, judge
    evidence, history trends); per-case output browsing stays with
    `promptfoo view`."""
    from src.dashboard.server import serve
    return serve(args.port, open_browser=args.open)


def cmd_cache(args) -> int:
    if args.op == "ls":
        for e in cache_tools.ls():
            print(f"{e['bytes']:>10}  {e['age_days']:>6}d  {e['file']}")
    elif args.op == "stat":
        s = cache_tools.stat()
        print(f"{s['entries']} entries · {s['bytes'] / 1e6:.1f} MB · {s['dir']}")
    elif args.op == "verify":
        suspects = cache_tools.verify()
        for s in suspects:
            print(f"SUSPECT {s['file']} — {s['problem']}")
        print(f"{len(suspects)} suspect entr{'y' if len(suspects) == 1 else 'ies'}")
        return 1 if suspects else 0
    elif args.op == "gc":
        n = cache_tools.gc(older_than_days=args.older_than, suspects_only=args.suspects)
        print(f"removed {n} entr{'y' if n == 1 else 'ies'}")
    return 0


# ----------------------------------------------------------------- parser —
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prompt-eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def live_flags(sp):
        sp.add_argument("--suite")
        sp.add_argument("--repeat", type=int)
        sp.add_argument("-j", "--jobs", type=int)
        sp.add_argument("--max-cost", type=float, help="raise the run ceiling (recorded)")
        sp.add_argument("--force", action="store_true", help="bypass gates (recorded)")
        sp.add_argument("--resume", nargs="?", const=True, help="promptfoo native resume")
        sp.add_argument("--filter-first-n", type=int)
        sp.add_argument("--require-clean", action="store_true")
        sp.add_argument("--dry-run", action="store_true",
                        help="print the full cost breakdown and exit before any call")

    sp = sub.add_parser("init", help="adopt inputs from the input/ drop box into a wired suite (free)")
    sp.add_argument("path", nargs="?", help="a prompt .md or folder (default: scan input/)")
    sp.add_argument("--example", help="stage a shipped example bundle (examples/<name>/bundle) "
                                      "into input/ and adopt it — e.g. support-reply")
    sp.add_argument("--as", dest="as_kind", choices=["prompt", "wrapped", "agent", "bundle"],
                    help="skip detection and treat the input as this type")
    sp.add_argument("--suite", help="suite id override (default: derived from the name)")
    from src.scaffold.templates import RUBRIC_LIBRARY
    sp.add_argument("--rubric", choices=sorted(RUBRIC_LIBRARY),
                    help="scaffold the rubric from a reviewed task-kind template "
                         "instead of the generic TODO anchors (explicit on purpose "
                         "— a silently wrong guess would poison calibration)")
    sp.add_argument("--yes", action="store_true", help="accept detection defaults, never prompt")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sp.add_argument("--print-config", action="store_true",
                    help="print the config edit to paste yourself instead of applying it")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("validate", help="offline gate (free)")
    sp.add_argument("--suite")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("doctor", help="environment sanity (free)")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("preflight", help="real-config smoke (~$0.05)")
    sp.add_argument("--suite")
    sp.add_argument("--max-cost", type=float)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_preflight)

    for mode in ("baseline", "graded", "compare", "confirm"):
        sp = sub.add_parser(mode)
        live_flags(sp)
        sp.set_defaults(fn=lambda a, m=mode: _run_live(m, a))

    sp = sub.add_parser("matrix", help="regenerate graded matrices")
    sp.add_argument("--suite")
    sp.set_defaults(fn=cmd_matrix)

    sp = sub.add_parser("calibrate", help="judge calibration vs goldens")
    sp.add_argument("--suite")
    sp.add_argument("--max-cost", type=float)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the full cost breakdown and exit before any call")
    sp.set_defaults(fn=cmd_calibrate)

    sp = sub.add_parser("verdict", help="blinded pairwise verdict on a compare results file")
    sp.add_argument("results", nargs="?", help="results.json path (default: latest compare/confirm run)")
    sp.add_argument("--target", required=True)
    sp.add_argument("--reason", help="required when repeating a verdict on unchanged inputs")
    sp.add_argument("--new-evidence", help="required for PROMOTE after a prior REJECT")
    sp.add_argument("--max-cost", type=float, help="raise the budget ceiling for this verdict (recorded)")
    sp.add_argument("--force", action="store_true", help="bypass the budget gate (recorded)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the full cost breakdown and exit before any call")
    sp.set_defaults(fn=cmd_verdict)

    sp = sub.add_parser("promote", help="apply a PROMOTE verdict")
    sp.add_argument("--suite", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_promote)

    sp = sub.add_parser("round", help="pipeline status + next action")
    sp.add_argument("--suite")
    sp.add_argument("--run", action="store_true",
                    help="execute the printed next command after confirmation (needs --suite)")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sp.set_defaults(fn=cmd_round)

    sp = sub.add_parser("why", help="explain each suite's blocked stage in prose")
    sp.add_argument("--suite")
    sp.set_defaults(fn=cmd_why)

    sp = sub.add_parser("monitor", help="model-drift canary: tiny fixed graded subset vs baseline")
    sp.add_argument("--suite", required=True)
    sp.add_argument("--max-cost", type=float)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the full cost breakdown and exit before any call")
    sp.set_defaults(fn=cmd_monitor)

    sp = sub.add_parser("perturb", help="draft paraphrase variants of existing probes (cheap dataset model)")
    sp.add_argument("--suite", required=True)
    sp.add_argument("--variants", type=int, default=3)
    sp.add_argument("--out", help="output dir (default: datasets/generated)")
    sp.add_argument("--max-cost", type=float)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the full cost breakdown and exit before any call")
    sp.set_defaults(fn=cmd_perturb)

    sp = sub.add_parser("gen-cases", help="draft ordinary-user test cases (cheap dataset model)")
    sp.add_argument("--suite", required=True)
    sp.add_argument("--count", type=int, default=5)
    sp.add_argument("--out", help="output dir (default: datasets/generated)")
    sp.add_argument("--max-cost", type=float)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the full cost breakdown and exit before any call")
    sp.set_defaults(fn=cmd_gen_cases)

    sp = sub.add_parser("spot-check", help="human spot-check of judge verdicts (free, offline)")
    sp.add_argument("--run", required=True, help="finished graded-tier run directory")
    sp.add_argument("--n", type=int, default=10, help="cells to sample (default 10)")
    sp.set_defaults(fn=cmd_spot_check)

    sp = sub.add_parser("pin", help="pin a failing cell into the permanent regression set")
    sp.add_argument("--run", required=True, help="run directory the failure came from")
    sp.add_argument("--cell", required=True)
    sp.add_argument("--note", help="why it was pinned (recorded in the file header)")
    sp.set_defaults(fn=cmd_pin)

    sp = sub.add_parser("history", help="verify the hash-chained audit ledgers")
    sp.add_argument("op", choices=["verify"])
    sp.set_defaults(fn=cmd_history)

    sp = sub.add_parser("graft", help="restore wiped state whose fingerprints still match")
    sp.add_argument("--snapshot", action="store_true", help="save a snapshot instead of restoring")
    sp.add_argument("--from-history", action="store_true",
                    help="rebuild state from outputs/history records (state file + backup lost)")
    sp.set_defaults(fn=cmd_graft)

    sp = sub.add_parser("report", help="regenerate a run's report")
    sp.add_argument("run_dir")
    sp.add_argument("--format", choices=["md", "json"], default="md",
                    help="md: report.md · json: versioned run.json to stdout")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("dashboard", help="local read-only dashboard (free)")
    sp.add_argument("--port", type=int, default=8642)
    sp.add_argument("--open", action="store_true", help="open the browser")
    sp.set_defaults(fn=cmd_dashboard)

    sp = sub.add_parser("cache")
    sp.add_argument("op", choices=["ls", "stat", "verify", "gc"])
    sp.add_argument("--older-than", type=float, dest="older_than")
    sp.add_argument("--suspects", action="store_true")
    sp.set_defaults(fn=cmd_cache)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"CONFIG ERROR — {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

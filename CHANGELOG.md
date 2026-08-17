# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

> **Honesty note:** this file was **reconstructed at v1.0.0** from the git
> history — it was not maintained release-by-release along the way, because
> there were no releases before v1.0.0. Commit hashes are cited so every
> claim is checkable. This file is deliberately excluded from the docs
> drift check: history must be allowed to name things that no longer exist.

## [1.0.0] — 2026-08-16

The finished form of the project. From here: **bug fixes only** — no feature
roadmap (see Status in the README).

### ⚠ Breaking / upgrade notes

- **The repo ships BARE.** The example project layer that once lived in
  `prompts/`, `datasets/`, `judges/`, and `fixtures/` was removed before
  v1.0.0; the project layer is yours (adopt content via `prompt-eval init`,
  or take the guided tour with `init --example support-reply`). Forks
  carrying the old demo suites should treat this as the major break it is.
- **Run `prompt-eval validate` once after upgrading.** The stage
  fingerprint gained keys (graded-matrix and regression-set SHAs, judge
  temperature, provider-resolved judge model id); older recorded stages
  read as stale — a free re-validate (plus `graft` for unaffected stages)
  brings them current. Nothing is lost; staleness is the design working.
- Judge-prompt wording changed once (spot-check/taxonomy work, 57f9e56):
  recalibrate before trusting new graded scores against old baselines.

### Added

**Correctness & state integrity**
- `confirmed` stage actually recorded; a verdict over a confirm run no
  longer wipes `promoted` (2c0acd1 — the bug the external review missed).
- Atomic state/manifest writes, snapshot rotation (once per process, keep
  10), `graft --from-history` disaster rebuild (70ed48c).
- Hash-chained durable ledgers + `history verify` tamper evidence (dba3bc9).
- Versioned `run.json` (schemaVersion 1), `report --format json`, secret
  redaction on durable artifacts (0b14e51).

**Spend governance**
- `--dry-run` on every paid command — full per-role breakdown, then exit
  before any call (0f267ca).
- Per-role and per-suite ceilings; rolling 24h/7d spend windows over a
  durable cross-run ledger (397bf67).
- Truncation detection (provider stop reason + token-cap heuristic) feeding
  the failure breaker; never auto-retried (130b2df).
- Value receipt: measured cache savings, blocked-spend ledger, live
  progress meter, one-line receipt per run (fe37274).

**Evidence quality**
- Calibration binds to the provider-resolved model id; free post-hoc drift
  gating; judge self-consistency reported from existing samples (2eff871).
- Verdict `min_cells` gate with the third outcome INSUFFICIENT_EVIDENCE
  (exit 2), distinct from REJECT (d81e42a).
- Permanent regression suite + `pin` — a fixed failure recurring fails any
  run loudly and blocks promotion (9d9980a).
- Human `spot-check` with blind-first labels, chained history, rolling
  agreement gate; failure taxonomy tags across judge + contracts (57f9e56).

**Authoring & ergonomics**
- Contract helpers library (`src/contracts_lib.py`) + template-variable
  fairness checks in validate (39e5ce4).
- `file://module.py:CONST` prompt sources with resolved-text fingerprints
  (fc52312).
- `why`, `round --run`, `monitor` drift canary, `perturb` paraphrase
  drafts, thin Python API (23653ff).
- Reviewed rubric template library + `init --rubric <kind>`; band-mock
  judge for $0 calibration wiring proofs; pricing freshness stamp
  (f5a32c0).

**Frontier (scoped honestly)**
- Per-suite HTTP provider via a materialized per-run promptfoo config;
  generation ledgered as cost_unknown, ceiling governs judge spend only
  (febc3f2).
- Multi-turn as labeled single-prompt simulation (`messages:` on cases),
  validated free, flagged in the manifest (465192b).

**Packaging, docs, example**
- MIT license, package metadata, four-way version-sync test, zero-spend CI
  (d42b089).
- `examples/support-reply/` worked example + `init --example` +
  WALKTHROUGH + $0 CI example-smoke job (611abab).
- README restructure (Status: complete, Non-goals, mermaid pipeline);
  `docs/` pages: comparison (dated, sourced), concepts glossary,
  calibration theory, providers; drift scan extended to all docs (c810ba0).
- **The Developer Handbook** (`docs/HANDBOOK.md`, 19 sections, tested
  references) (4b57964).
- Reproduction Dockerfile (+ `.dockerignore` excluding `.env`) and the
  label-gated PR-workflow example (a2a3ec9).

### Changed

- Cost estimates: measured-history basis everywhere, workload-matched for
  calibration; static config numbers demoted to no-history fallback.
- `config.pricing()` tolerates metadata rows (`last_verified`) and falls
  back to the most expensive *model* row for unknown models.
- Stage fingerprints extended (see upgrade notes above).

### Fixed

- The `confirmed`-stage recording gap and the verdict-on-confirm
  `promoted` wipe (2c0acd1).
- Calibration temperature-fingerprint mismatch that staled fresh
  calibrations (70ed48c).
- Multi-suite history records no longer mis-attribute per-suite cost rates
  (dedfdd8, pre-branch).
- Holdout reservation is hash-ranked, not positional — cherry-pick-proof
  (e8c1137, pre-branch).

### Removed

- Nothing at v1.0.0 itself. The pre-1.0 removal of the bundled example
  project layer (the "bare ship") is flagged under breaking notes above
  because forks feel it as removal.

### Deliberately rejected (recorded, final)

PyPI publishing; GHCR image publishing; `.j2` prompt templates; default
p-value promotion gate; model-drafted golden fixtures (`gen-fixtures`);
plugin API / pytest plugin; scheduler for `monitor`; version
single-sourcing. Reasoning: README Non-goals + `docs/HANDBOOK.md` §17–18.

[1.0.0]: https://github.com/AlqarniMohammed/prompt-eval-engine/releases/tag/v1.0.0

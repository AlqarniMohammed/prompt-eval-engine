# RUNBOOK — operating directives

The checklist form of every rule the $130 post-mortem produced
([LESSONS-LEARNED.md](LESSONS-LEARNED.md)). The state machine enforces the sequence;
this file is for the human-in-the-loop decisions and the rules that live above any
single run. When a directive here conflicts with convenience, the directive wins.

## Hard rules (never waived)

- [ ] **API-key transport only.** `ANTHROPIC_API_KEY` in `.env` (plus `OPENAI_API_KEY`
      for a cross-family judge); never subscription auth.
- [ ] **Validate before any paid run.** `validate` green, then `preflight` for the
      target suite, before the first live dollar.
- [ ] **Subset-first.** A full `graded` run is gated on a successful
      `--filter-first-n 2` smoke for the same suite content and models. The gate is in
      code; this line is here so you don't `--force` it out of habit.
- [ ] **Every live command runs behind the $1.00 ceiling.** Raising it is an explicit
      `--max-cost N` on that one command (size it from the `--dry-run`
      breakdown first) — never an env default, never editing
      `governance.max_run_cost_usd`.
- [ ] **Never override or re-roll a pairwise REJECT.** A repeat verdict on unchanged
      inputs requires `--reason`; PROMOTE after a REJECT requires `--new-evidence`. If
      you're reaching for either, the honest move is a new candidate SHA.
- [ ] **Judge is valid only behind a green calibration record.** No graded run on a
      judge/rubric combination whose calibration is stale. Judge == generation model is
      a hard error, not a preference. A judge model change — including switching to a
      cross-family `openai:` judge — invalidates every suite's calibration at once.
      The judge's temperature is fingerprinted too (a sampling change is a judge
      change); the graded judge-prompt *text* is not — after pulling an engine upgrade
      that edits the prompt builder, recalibrate by hand before trusting graded scores.
- [ ] **Deterministic contracts stay alongside the judge.** The canary asserts caught
      an AWS-name leak the judge scored 5/5. Never trade contracts away for judge
      coverage.
- [ ] **Campaigns run `--require-clean`.** 40 of 41 post-mortem paid runs happened on a
      dirty tree; the one config we most needed was unrecoverable.
- [ ] **Dataset drafts are drafts.** `gen-cases` output must be human-reviewed before
      merging into a spec — the cheap model's job is realism, yours is correctness.

## Known engine gaps (open as of 2026-08-15 — compensate by hand)

- [ ] **A watchdog kill loses promptfoo's generation cache** (the process group dies
      before the cache flushes). Compensate: set `--max-cost` honestly *before* the run
      rather than letting the watchdog be the ceiling; a killed run re-pays full
      generation price.
- [ ] **The cost gate trusts measured-estimate records across judge/config changes.**
      After changing the generation model, judge model, or repeat count, treat the
      gate's estimate as stale: expect real cost up to ~2× the estimate and size
      `--max-cost` accordingly (the $1.07 graded-smoke kill came from a stale
      haiku-era record).
- [ ] **The cross-family judge path (`openai:`) has never taken a live call** — it
      shipped under a spend freeze with offline tests only. Its first use must be a
      calibration (which the state machine forces anyway); expect to debug SDK-shape
      drift there, not in a graded run.

This list holds only what still needs hand-compensation during a run; closed
gaps live in the incident tables of [LESSONS-LEARNED.md](LESSONS-LEARNED.md)
and [docs/HANDBOOK.md](docs/HANDBOOK.md). One closed gap is recorded only here:
the failure breaker once counted judge-scored below-threshold cases as errors —
promptfoo copies assert-failure reasons into `result.error`, so a valid graded
baseline was killed at "60% error rate" ($2.02 lost). Fixed: only real ERRORs
trip the breaker; graded fails ride the separate `passed` field.

## Per-suite campaign cycle

Run `uv run prompt-eval round --suite <id>` at every step — it tells you where you are
and the next required command. The cycle, with its two decision points:

1. [ ] `validate` — free; must be green on the current tree.
2. [ ] `calibrate --suite <id>` — all samples in band. If NO-GO, fix the rubric/judge,
       never the band.
3. [ ] `preflight --suite <id>` — ~$0.05 real-config smoke.
4. [ ] `graded --suite <id> --filter-first-n 2` — the smoke the full run is gated on.
5. [ ] `graded --suite <id>` — size `--max-cost` from *measured* history for this
       model+judge combo; stale-estimate rule above applies. After the full baseline,
       run the judge↔human agreement sample (section below) before iterating on it.
6. [ ] `compare --suite <id>` — candidate vs production, same matrix.
7. [ ] **DECISION 1:** `verdict --target <dimension>` — blinded pairwise (defaults to
       the newest compare results). Accept the verdict as written; a third outcome,
       INSUFFICIENT_EVIDENCE (under `min_cells`, or over an opted-in `max_sign_p`),
       means regenerate a larger matrix and re-compare — it is not a REJECT and does
       not require `--new-evidence` to retry. The table prints a
       sign-test p-value and a 95% CI on the target delta — they are reported, not
       gated on: with the default 6 iteration cells, p < 0.05 needs a unanimous sweep,
       so treat them as a reminder of how little the small matrix can prove, and scale
       the matrix if the decision needs more power.
8. [ ] `promote` — only on PROMOTE.
9. [ ] **DECISION 2:** `confirm --suite <id>` — holdout cells only. A promotion that
       fails confirmation is reverted, not argued with. A clean run records the
       `confirmed` stage; a run with findings records nothing and exits nonzero.

## Regression pinning

Every real-world failure that gets fixed is pinned BEFORE the fix ships:
`prompt-eval pin --run <dir> --cell <id> --note "<what broke>"`. Pinned cases
live in `datasets/regression/`, run in every graded-tier pass, and a recurrence
fails the run loudly no matter the aggregate. After pinning: free `validate`,
then `graft`.

## Durable-artifact hygiene

Everything written under `outputs/history/` (git-tracked, outlives every run) is
secret-scrubbed on write (API-key/token/private-key patterns, plus any extra
`governance.redaction.patterns`); `governance.redaction.history_raw_text:
keep|hash|omit` additionally controls whether full prompt/output text lands in
durable records. Per-run scratch under `outputs/runs/` is deliberately untouched.

## Judge↔human agreement (per campaign)

Calibration now also records the judge's **self-consistency** (exact agreement +
mean spread across the k samples) and the **provider-resolved model id** it ran
against; a later run resolving a different id warns, and gates the next graded
run until recalibration (`doctor` compares the preflight-resolved id for free).
Calibration proves the judge lands golden fixtures in band; it says nothing about
whether the judge agrees with a human on *real* outputs. That protocol now lives
in code: after each full `graded` baseline, run
`prompt-eval spot-check --run <dir> --n 10` — it hash-samples distinct cells from
the run, takes your blind pass/fail labels first, reveals the judge's scores
after, prints the session's per-dimension agreement, and chains everything into
`outputs/history/spot-checks.jsonl`; `round`/`doctor` warn when the rolling rate
drops below `graded.calibration.min_human_agreement` (default 90%) once ~30
labels exist. Three rules the tool can't enforce:

- [ ] The sampler spreads over distinct cells but does **not** stratify by axis —
      eyeball the printed sample list for axis coverage before labeling, and rerun
      if one axis dominates.
- [ ] Below target, treat the gap as a rubric-anchor defect — the per-dimension
      lines say where: fix the anchors or rubric wording, `calibrate`, re-baseline
      — never bend your own labels toward the judge's.
- [ ] Any judge change (model, temperature, rubric) is trusted only after agreement
      holds on the first post-change baseline sample.

## Model-stabilization sweep (picking the production model)

Goal: find where scores stop improving with model tier, then ship the cheapest model
past the knee. Costs a few smoke runs, not a campaign:

1. [ ] Pick 1–2 representative suites; judge stays fixed and calibrated throughout.
2. [ ] For each candidate model:
       `EVAL_MODEL=<model> uv run prompt-eval preflight --suite <id>` (the preflight
       fingerprint pins the generation model, so each leg needs its own), then
       `EVAL_MODEL=<model> uv run prompt-eval graded --suite <id> --filter-first-n 2`.
3. [ ] Compare per-dimension means and spread across the run manifests
       (`outputs/runs/`) — same cells, same judge, only the generation model varies.
4. [ ] Scores flattening between adjacent tiers (e.g. 8.9 → 9.0) = past the knee; a
       full-point jump = below it. Set `agents.generation.model` and
       `project.production_model` to the cheapest model past the knee, then run the
       real baseline on it.
5. [ ] Record the sweep table (model → scores → cost) in the project log; the next
       model-choice discussion starts from data.

## Before a multi-suite campaign

- [ ] Tree clean, `--require-clean` on every live command.
- [ ] All target suites `calibrated` on the *current* judge model.
- [ ] Budget declared up front: expected total, per-run `--max-cost`, and the number
      you'll stop at.
- [ ] `cache verify` green — a poisoned cache entry once replayed hollow outputs as
      passes.

## Monitoring a live run

Open `uv run prompt-eval dashboard --open` in a second terminal before starting a
paid run — what the live panel shows (spend meter, ETA, truncation alerts, stalled
flag) is described in the [README's dashboard section](README.md#local-dashboard). The
operating rule: the dashboard is read-only and localhost-only; it can never touch
a run's state or its lock. To verify it for free on a fresh clone: copy the shipped
sample records in (`cp tests/fixtures/dashboard/history/*.json outputs/history/`
— remove them afterwards, the directory is git-tracked) for the trend panels,
and run the free `validate` in another terminal to watch the live panel pick
it up.

## After every run

- [ ] Read the report, not just the pass/fail line — canary and contract failures are
      findings even when scores are high.
- [ ] Cost line in the manifest vs your estimate; a >2× miss means a stale estimate
      record — note it before the next run.
- [ ] Anything surprising gets an incident row (incident → root cause → fix) in
      [LESSONS-LEARNED.md](LESSONS-LEARNED.md), not a mental note.

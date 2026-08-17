# Lessons Learned

Everything in this engine exists because something went wrong first. This file is the
post-mortem of the original eval campaign (August 2026, built on a JS kit this engine
replaced) that spent **$130.15 of measured API credit** — several times the intended
budget — plus the incidents found since. Each entry records what happened, the root
cause, why it wasn't avoided at the time, and the rule now enforced *in code* because
of it.

If you only read one thing: **output tokens were 80.5% of all spend**. Model choice and
output length dominate every other cost decision, and every "process" failure below was
an amplifier on top of that.

## 1. The accounting

| | Calls | Tokens | USD |
|---|---:|---:|---:|
| claude-opus-5 generation | 1,127 | — | **$103.77** (80%) |
| claude-sonnet-5 judging | 373 | — | $26.38 (20%) |
| **Total (2026-08-14 → 08-15)** | **1,500** | 15.37M | **$130.15** |

- Graded baselines: $99.68 spent vs ~$46 legitimate — the delta was process failure, not model cost.
- One suite's "death march": 6 rounds, $51.25 spent, $15.69 useful.
- With the fixes below, a full 6-suite baseline that measured $51.65 now targets **$8–15**.

## 2. Cost incidents — what happened, root cause, why it wasn't avoided, the fix

| # | Incident | Cost | Root cause | Why not avoided | Rule now in code |
|---|----------|------|-----------|-----------------|------------------|
| 1 | **Concurrent-run starvation.** 5 runs live at once; two ran 77–80 min with mass timeouts; one made 114 generation calls and 0 judge calls | ~$32 | No mutex — humans could launch overlapping runs, and did | Nothing signaled that a run was already live | Run lock on **every** spending entrypoint |
| 2 | **Orphaned child process.** A wrapper-level `pkill` killed the parent; the child billed $12.76 with zero artifacts | ~$13 | Child not in the parent's process group | The wrapper looked like it worked — silence read as success | Process-group spawn + kill; a manifest is written even for crashed runs, so orphan spend is never invisible |
| 3 | **Hollow-output poisoned cache.** A tool-loop that exhausted max-turns returned intro-only text as "success"; it was cached, judged 1–2, and replayed into later comparisons | ~$7 | Loop exhaustion was silent; the cache accepted the output; cache keys omitted output-determining params | The output *looked* like a result; only score archaeology exposed it | Exhausted outputs throw; cache keys include every output-determining parameter; `cache verify` codifies the hollow-entry heuristic |
| 4 | **Overload storm.** 22 `overloaded_error` events in one run; blind retries re-billed calls that had already consumed tokens | ~$8 | Retry logic didn't distinguish billed from unbilled failures | Retries are "best practice" — nobody priced them | Free-only retries (429/5xx/connection), with backoff + jitter |
| 5 | **Truncation-retry.** 75 calls hit the exact output-token cap: a turn cut mid-document re-emits the entire document | ~$34 (28% of lifetime spend) | 16k/32k caps too small for document-writing suites | The cap was set once, globally, and never revisited per suite | 64k cap; exact-cap alerts on the ledger; judge cap raised past the JSON-truncation point |
| 6 | **Budget gate that never fired.** Static $0.25/trial guess under-predicted document suites 4× | enabler of 1–2 | Estimates were guessed in config, never reconciled with measurement | The banner *showed* a number, which felt like control | Estimates come from **measured history**, cache-aware; static guesses only when no history exists |
| 7 | **No mid-run ceiling.** Runaway runs were discovered by humans reading logs | enabler | Nothing watched cumulative spend during a run | "The estimate was fine" — see #6 | Spend kill-switch + **failure-rate breaker** (a 100%-failing run burns to the cap on dollars alone) |

## 3. Operational incidents — beyond cost

| # | Incident | Root cause | Rule now in code |
|---|----------|-----------|------------------|
| 8 | **A REJECT verdict was re-rolled until it passed.** An identical candidate was re-compared after a REJECT; a handful of re-rolled judge calls flipped it to PROMOTE | The pairwise judge was stateless — nothing distinguished "re-ran after a fix" from "re-ran until it passed" | **Verdict idempotency**: verdicts keyed on `(candidateSha, currentSha, rubricSha, judgeModel)`; a repeat needs `--reason`; PROMOTE after REJECT needs `--new-evidence` |
| 9 | **A whole run died in 1 second and preflight couldn't have caught it.** A "streaming required" API error killed 15/15 rows; the doctor had smoked a different model at different caps | Preflight didn't exercise the real configuration | `preflight` = one real call with the configured model + caps through the real provider path, required before campaigns |
| 10 | **Nobody saw a 77-minute starvation live**, and total-failure runs printed as COMPLETED | No anomaly detection; logs hand-named and overwritten | Per-run directories; error-rate alerts; failed runs labeled FAILED |
| 11 | **40 of 41 paid runs ran on a dirty tree**; the config behind incident 9 is unrecoverable | Manifests didn't capture effective state | Manifests record effective params, every env override used, content SHAs, and the verbatim config when dirty; `--require-clean` for campaigns |
| 12 | **The pipeline lived in memory.** Only 2 of 8 documented sequence links were enforced; ~19 manual steps per optimization cycle | Rules lived in docs | The per-suite **state machine**: `validate → calibrate → baseline → compare → promote → confirm`, refusing stale transitions; `round` prints the next required command |
| 13 | **Docs drifted until they lied.** A method doc described wrong paths, phantom env vars; a config header claimed "no API keys" while loading the API provider | Guardrail commits never touched docs | `tests/test_docs_drift.py`: every path, env var, command, and flag in the docs must exist in code, or CI fails |
| 14 | **Calibration and pairwise spend was invisible** unless an operator remembered to export a run ID | Spending scripts didn't self-register | Every spending entrypoint auto-registers a run dir and writes a ledger |
| 15 | **The calibration cost gate false-aborted twice** — its estimate borrowed a fleet-wide golden average and a mismatched judge rate | Estimate reused history from a different workload | Exact per-suite call counts; workload-matched rates; abort messages state their basis |
| 16 | **The state snapshot held only the last suite.** A config edit invalidated all six suites; the pre-wipe backup was overwritten once per suite as `validate` re-recorded them sequentially, so restore recovered 1 of 6 (the rest were reconstructed from tracked history records) | Snapshot fired before *every* wipe instead of before the *first* | Auto-snapshots fire once per process; `graft` restores only entries whose own fingerprints still match, and the durable history in `outputs/history/` remains the source of last resort |

## 4. Did we reinvent the wheel? (the honest answer)

Partly — and the audit quantified where. ~1,100 lines of the original kit re-implemented
things promptfoo already shipped: an agentic provider, a content cache, crash recovery
(`--resume`), matrix expansion, cost math, an HTML viewer. **Audit the installed tool's
source before hand-rolling anything** — the features existed the whole time.

What survived the audit as genuinely absent from promptfoo *and* the surveyed market
(Inspect AI, DeepEval, Braintrust, Langfuse, LangSmith, OpenAI Evals, Weave, Phoenix,
Harbor) is exactly what this engine's Python layer contains:

- **Run-level cost governance** — measured pre-run gate, mid-run kill-switch, run lock.
- **Judge calibration against golden fixtures** — no surveyed tool ships it; 2026 research
  measures judge position-flip rates at 25–50%.
- **Blinded position-swapped pairwise promotion with verdict idempotency.**
- **The pipeline state machine** with content-hash staleness.

The prompts, fixtures, rubrics, and domain checks are a test suite, not a framework —
every team writes their own.

## 5. Rules going forward (enforced in code, not memory)

1. Never concurrent generation runs — run lock on every spending entrypoint.
2. Kill by process group; write a manifest even for crashed runs.
3. Estimates come from measured history, cache-aware.
4. $1.00 default ceiling per run; raising it is an explicit, recorded `--max-cost`.
5. Watch failure rate, not just dollars.
6. Preflight the real configuration before any campaign.
7. Validate offline before every live run — the free gate is mandatory.
8. **Cheap models by default**: the generator under test matches what you ship; the
   dataset generator is the cheapest model that writes *realistic* cases; the judge is a
   different model (ideally a different family), valid only behind calibration.
9. Hollow/exhausted outputs must throw — never cached, never judged.
10. Cache keys include every output-determining parameter.
11. Verdicts are idempotent — re-rolling a REJECT on unchanged inputs is not evidence.
12. **Subset-first**: no full run before a small run has succeeded on the same content
    and models (enforced by the `smoked` gate).
13. Audit the installed tool's source before hand-rolling anything.
14. Sequencing lives in a state machine; docs are drift-checked; every spend is on the ledger.

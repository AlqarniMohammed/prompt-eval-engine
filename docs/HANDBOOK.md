# The Developer Handbook

Every design decision in this engine, explained well enough to defend,
maintain, and hand over. This is the document to read when you wonder *why*
something is the way it is — the [README](../README.md) tells you what to
run; this tells you why running anything else was rejected.

Written against the finished v1.0 engine. Structure:

1. [How to read this, and what "verified" means](#1-how-to-read-this-and-what-verified-means)
2. [The $130.15 post-mortem — the spine](#2-the-13015-post-mortem--the-spine)
3. [Why promptfoo underneath](#3-why-promptfoo-underneath)
4. [The three model roles](#4-the-three-model-roles)
5. [The state machine](#5-the-state-machine)
6. [Calibration theory](#6-calibration-theory)
7. [Verdicts and their statistics](#7-verdicts-and-their-statistics)
8. [Holdout and confirm](#8-holdout-and-confirm)
9. [The cost model](#9-the-cost-model)
10. [Caching, idempotency, ledgers](#10-caching-idempotency-ledgers)
11. [Bare-ship and the init design](#11-bare-ship-and-the-init-design)
12. [Graft and snapshots](#12-graft-and-snapshots)
13. [The dashboard's read-only doctrine](#13-the-dashboards-read-only-doctrine)
14. [The `src` package-name wart](#14-the-src-package-name-wart)
15. [The zero-spend test architecture](#15-the-zero-spend-test-architecture)
16. [Docs-drift philosophy](#16-docs-drift-philosophy)
17. [Known gaps and accepted tradeoffs](#17-known-gaps-and-accepted-tradeoffs)
18. [Release and distribution decisions](#18-release-and-distribution-decisions)
19. [Defending the design — the FAQ](#19-defending-the-design--the-faq)

---

## 1. How to read this, and what "verified" means

Three kinds of claim appear in this repo's documentation, and they carry
different weight:

- **Enforced in code** — the strongest kind. The claim has a function you can
  point at and a test that fails if it stops being true. "The judge cannot be
  the generation model" is this kind: `src/config.py` raises, and
  `tests/test_config.py` proves it raises. When this handbook says
  *enforced*, it names the file.
- **Verified at a point in time** — true when checked, dated, and decaying.
  The market survey behind "no surveyed tool ships judge calibration" is
  this kind; it lives in [comparison.md](comparison.md) under a staleness
  banner. Repeat these claims *with their date* or not at all.
- **Judgment calls** — decisions that could have gone another way, recorded
  with their reasoning so a future maintainer can re-decide with the same
  information. The band numbers in calibration are this kind. This handbook
  exists mostly for these.

The discipline that keeps the three honest: anything enforceable got
enforced (rules live in the state machine and the gates, not in prose);
anything dated got a date; anything else says why. The docs themselves are
drift-checked (§16), so a path or flag named here exists — the *reasoning*
is the only part tests can't check, which is why it's written down.

A reading suggestion: §2 first, always. Every other section is a footnote to
the post-mortem.

## 2. The $130.15 post-mortem — the spine

In August 2026, the eval campaign this engine replaced spent **$130.15** —
roughly 3× its intended budget — across 1,500 calls in two days. The full
accounting lives in [LESSONS-LEARNED.md](../LESSONS-LEARNED.md); the single
most important number is that **output tokens were 80.5% of all spend**, and
every process failure was an amplifier on that base rate.

The engine is that post-mortem, executed. The table below maps every
incident to the exact guardrail that now exists because of it — file and, where
one applies, config key. When you're tempted to remove a guardrail, this
table tells you which invoice you're re-opening.

| # | What happened | Cost | Guardrail now, and where |
|---|---|---|---|
| 1 | Five runs live at once; starvation, mass timeouts, one run made 114 generation calls and 0 judge calls | ~$32 | Run lock on every spending entrypoint — `src/utils/run_lock.py`, acquired in `src/runner.py` before any call |
| 2 | A wrapper-level kill orphaned the child, which billed $12.76 with zero artifacts | ~$13 | Process-group spawn/kill — `src/utils/proc.py`; the manifest is written *before* the first call so even a crash is accounted |
| 3 | A max-turns-exhausted "success" (intro text, no files) was cached, judged 1–2, and replayed into later comparisons | ~$7 | Hollow outputs raise — `src/loaders/bundle_transform.py`; suspect-entry heuristics in `src/utils/cache_tools.py` (`cache verify`) |
| 4 | 22 overload errors; blind retries re-billed calls that had already consumed tokens | ~$8 | Free-only retries (429/5xx/connection) with backoff — `src/utils/model_client.py` |
| 5 | 75 calls landed exactly on the output-token cap; each truncated document re-emitted whole | ~$34 | 64k generation cap (`agents.generation.max_tokens_per_turn`), 24k judge cap (`agents.judge.max_tokens_per_turn`), exact-cap ledger alerts in `src/hooks.py`, truncations feed the breaker (`governance.failure_breaker.count_truncations`) — and **never** auto-retry |
| 6 | The budget gate used a static guess that under-predicted document suites 4× | enabler | Estimates from measured history, cache-aware — `src/utils/cost_tracker.py` (`measured_estimates`); `governance.estimate` is the no-history fallback only |
| 7 | Nothing watched cumulative spend mid-run; runaways were found by humans reading logs | enabler | The watchdog: spend kill-switch + failure breaker — `src/utils/proc.py`, thresholds in `governance.failure_breaker` |
| 8 | A REJECT verdict was re-rolled until it flipped to PROMOTE | trust | Verdict idempotency — `src/science/pairwise.py`; keyed on (candidateSha, currentSha, rubricSha, judgeModel); repeats need `--reason`, PROMOTE-after-REJECT needs `--new-evidence` |
| 9 | A campaign died in 1 second on a "streaming required" error preflight couldn't have caught — it had smoked a different model at different caps | waste | `preflight` runs one real case with the *configured* model and caps through the real provider path — `src/runner.py` |
| 10 | A 77-minute starvation ran unseen; total-failure runs printed COMPLETED | trust | Per-run directories, status events, honest FAILED labels — `src/runner.py`, `src/utils/status.py`, `src/reports/summary.py` |
| 11 | 40 of 41 paid runs ran on a dirty tree; the config behind incident #9 is unrecoverable | trust | Manifests record content SHAs, effective agents, every env override used, and the verbatim (redacted) config when dirty — `src/runner.py` (`_build_manifest`); `--require-clean` for campaigns |
| 12 | The pipeline lived in memory: 2 of 8 documented sequence links enforced, ~19 manual steps per cycle | waste | The state machine — `src/state.py`; `round` prints the one next required command, `round --run` executes exactly it |
| 13 | Docs drifted until they lied ("no API keys" atop a file that loaded the key) | trust | `tests/test_docs_drift.py` — every path, env var, command, and flag in the scanned docs must exist, or CI fails |
| 14 | Calibration/pairwise spend was invisible unless an operator remembered to export a run id | trust | Every spending entrypoint self-registers a run dir and ledgers into it — `src/utils/cost_tracker.py`, `_run_dataset_side_command` in `src/runner.py` |
| 15 | The calibration cost gate false-aborted twice on borrowed, workload-mismatched estimates | friction | Exact per-suite call counts (`calibration_judge_calls`) + calibration-specific measured rates (`measured_calibration_judge_rate`) — judge workloads never share an estimate basis |
| 16 | The pre-wipe state backup was overwritten once per suite; restore recovered 1 of 6 suites | trust | Snapshots fire once per process into `outputs/.state-snapshots/` (keep 10); `graft` restores only fingerprint-matching entries; durable history remains the last resort — `src/state.py` |

Three patterns worth extracting, because they generalize past this project:

1. **Every dollar of waste had a silent failure underneath.** Orphans that
   looked like success, hollow outputs that looked like results, retries that
   looked like resilience. The engine's reflex is therefore *loudness*:
   crashes get manifests, aborts get printed reasons and ledger entries,
   forced gates are recorded.
2. **Guessed numbers feel like control.** The $0.25/trial banner (incident
   #6) *displayed* a number and thereby ended the conversation. Measured
   numbers — even measured savings, even measured refusals — are a design
   requirement here, not an optimization.
3. **Rules that live in memory don't survive contact with a second operator**
   (incident #12). Anything phrased "always do X before Y" either became a
   state-machine edge or should be treated as unenforced folklore.

## 3. Why promptfoo underneath

The original JS kit re-implemented ~1,100 lines of what promptfoo already
shipped: an agentic provider, a content cache, crash recovery, matrix
expansion, cost math, an HTML viewer. The audit that found this
(LESSONS-LEARNED §4) produced the rule **audit the installed tool's source
before hand-rolling anything** — and this engine is that rule applied:
every live run is a `promptfoo eval` child process, and the Python layer
contains only what the audit verified absent from promptfoo *and* the
surveyed market (dated survey in [comparison.md](comparison.md)):
run-level cost governance, judge calibration, blinded idempotent verdicts,
and the state machine.

What delegation buys, concretely: the eval matrix and its cache (with
`--resume`), the claude-agent-sdk provider with per-test workspaces and
tool permissioning, result files a whole ecosystem reads, and `promptfoo view`
for per-case browsing. What it costs, and how each cost is handled:

- **A child process boundary.** The runner cannot reach into the eval loop,
  so control is exercised from outside: the watchdog polls the ledger and
  status stream and kills the process group (§2, incidents #2/#7). This is
  cruder than in-loop control and entirely sufficient.
- **An environment-shaped interface.** Configuration crosses the boundary
  via env vars (`EVAL_SUITE`, `EVAL_RUN_DIR`, `EVAL_REPEAT`, ...) and Python
  generator hooks (`src/loaders/pf_tests.py`). The child env is a
  deterministic allowlist because the provider hashes its env into cache
  keys — shell noise would silently kill every cache hit.
- **promptfoo's quirks become load-bearing knowledge.** Two examples that
  cost real debugging time: test-level `provider:` dicts from Python
  generators are silently ignored (per-test config must ride in test
  `options`; config-level providers are the only providers — which is why
  http suites materialize a per-run config), and the generation cache
  fingerprints file *mtimes*, so workspaces pin content-derived mtimes or
  nothing ever cache-hits. Both are documented at their point of use.
- **A version pin.** promptfoo is exact-pinned in `package.json`; `doctor`
  refuses a mismatched install. Upgrades are deliberate events, not drift.

The alternatives considered and rejected: *build the runner ourselves* (the
kit had proven this path — 1,100 redundant lines and none of the governance),
*Inspect AI / DeepEval* (model-benchmarking shape, not prompt-promotion
shape; no agent-workspace provider equivalent), *hosted platforms* (spend
governance capped at key/month granularity; artifacts leave the machine).

## 4. The three model roles

Declared in `config/eval_config.yaml` under `agents:`; enforced in
`src/config.py`.

**Generation** is the model under test, and its one rule is *fidelity*:
match `project.production_model` or you're evaluating a system you don't
ship (`doctor` warns). It runs through the real provider path with the real
caps because incident #9 proved that smoking anything else proves nothing.

**Judge** must differ from the generation model. This is a hard error, not
lint: self-grading bias is not a subtle effect (models rate their own
output generously and 2026 research measures judge position-flip rates at
25–50%), and the original kit shipped a whole milestone with self-grading
buried as a warning nobody read. The judge is valid *only* behind a green
calibration (§6), runs at temperature 0 by default (graders should be
deterministic — sampling noise otherwise lands directly in the scores),
and both the temperature and the provider-resolved model id are part of the
calibration fingerprint. Cross-family judging (`openai:` prefix) exists for
same-family-bias hygiene, with its own caveat in [providers.md](providers.md).

**Dataset** (the `gen-cases` / `perturb` model) is deliberately the
cheapest one that writes *realistic* text, and this is the most
counterintuitive call in the config: a stronger model here makes the eval
**worse**. It writes the inputs going INTO the system, not the outputs
being judged — and a strong model "helpfully" writes polished, best-practice
prompts no real user would type. An eval fed idealized inputs measures the
wrong distribution. Its output is always a draft for human review under
`datasets/generated/`, never auto-wired.

Why roles rather than one model setting: the three jobs have different
correctness criteria (fidelity / independence / realism), different price
sensitivities, and different failure modes — collapsing them was how the
original campaign ended up judging opus with sonnet while generating with
opus at 80% of all spend.

## 5. The state machine

`src/state.py`. Six recorded stages per suite — validated, calibrated,
baselined, compared, promoted, confirmed — with three properties that do all
the work:

**Predecessor gating.** Each paid command names the stage it requires;
`state.require()` refuses when the predecessor is missing, stale, or (for
calibration) not green. The refusal message states the exact reason and the
command that earns it — a gate that can't explain itself just gets
`--force`d (which is allowed, and *recorded* into the run manifest, because
an operator override you can see is safer than one you've made attractive
to hide).

**Fingerprint staleness.** A recorded stage carries content SHAs of
everything it depended on: prompt text (resolved text for `file://`
constants), rubric, config, graded matrix, regression set, judge model +
temperature + provider-resolved id. Stale means *any* of those moved — the
stage isn't deleted, it stops counting, and `why` names the exact key that
moved. This is how "re-validate after prompt surgery" became physics
instead of advice (incident #12).

**Successor wiping.** Earning a stage wipes everything after it: a new
baseline invalidates the old compare, a new compare invalidates the old
promotion. The wipe is what makes the records *mean* something — a
`promoted` entry can only exist downstream of the exact baseline it beat.
The recovery story for over-eager wipes is `graft` (§12).

Two design notes. The state file (`outputs/.state.json`) is a working copy,
not the truth — the durable truth accumulates in `outputs/history/`, which
is why a lost state file is an inconvenience, not a disaster. And the
`smoked` gate (subset-first: a full graded run requires a successful
`--filter-first-n` run on the same content and models) is deliberately a
side-record rather than a seventh stage — it gates one transition, expires
with content changes, and would otherwise double the ceremony of the stage
table for one rule.

## 6. Calibration theory

The full treatment lives in [calibration.md](calibration.md); this section
is the *why it's shaped this way* summary and what it defends against.

The problem: an LLM judge emits confident numbers unconditionally. Nothing
about a "4" tells you whether the judge can distinguish good from bad *in
your domain, against your rubric*. Every downstream decision — baselines,
compares, promotions — inherits whatever the judge's numbers actually mean.

The mechanism: before any graded run, the judge scores golden fixtures a
human already labeled (pass / fail / mid), k samples each, and **every
sample** must land in its band — `pass*.txt` ≥ `graded.calibration.pass_min`
on every dimension, `fail*.txt` ≤ `graded.calibration.fail_max` on its
declared target dimension, mid means strictly between. All-samples (not
mean) because variance in a temperature-0 grader is itself a finding; bands
(not exact scores) because two honest judges may disagree 4-vs-5 on a good
output and calibration must fail on *incompetence*, not temperament. The
numbers 4 and 2 are judgment calls defended in calibration.md: 5-only would
force flawless-only fixtures, 1-only would force cartoon failures, and both
would calibrate the judge against a distribution your prompt never
produces.

What it defends against, concretely: rubric anchors the judge reads
differently than you meant (the most common miss), judge-model swaps behind
stable API names (the resolved id is fingerprinted; `doctor` compares it
free against preflight's record, and every paid run re-verifies post-hoc),
temperature drift, and rubric edits (sha-fingerprinted). What it cannot
defend: fixture curation errors (goldens are human-labeled by design — §19
Q8), and live-output distribution shift, which is what `spot-check`'s
rolling human agreement and `monitor`'s canary exist for.

Calibration cost is deliberately first-class: it spends judge calls, so it
sits behind the same gates as everything else, with its own
workload-matched estimate basis (incident #15 — calibration judges short
golden bundles at ~1/10 the token cost of a full-transcript graded call;
sharing a rate between those workloads false-aborted twice).

## 7. Verdicts and their statistics

The promotion decision is the highest-stakes moment in the pipeline and the
one most exposed to motivated reasoning — the person running the compare
*wants* their candidate to win. The verdict machinery
(`src/science/pairwise.py`) is therefore built as a chain of
bias-eliminations:

**Blinding.** The pairwise judge sees two outputs labeled only A and B —
never "current" and "candidate". You cannot flatter a label you cannot see.

**Position swapping.** Every cell is judged twice with the positions
swapped, and a cell counts as a win only when *both orders agree*. Judge
position bias is measured in the 25–50% flip-rate range in 2026 research;
requiring both-order agreement converts that from a thumb on the scale into
a discard.

**Asymmetric promotion thresholds.** PROMOTE requires
`graded.promotion.min_pairwise_wins` both-order candidate wins *and zero*
both-order current wins. The asymmetry is intentional: the cost of a false
promotion (shipping a regression) exceeds the cost of a false rejection
(keeping the status quo), so ties and splits keep the incumbent.

**Idempotency** (incident #8). A verdict is keyed on (candidateSha,
currentSha, rubricSha, judgeModel). Re-running it on identical inputs
requires `--reason`; flipping a REJECT to PROMOTE additionally requires
`--new-evidence`. Re-rolling until the dice cooperate is thereby a recorded,
explained act instead of a quiet one. INSUFFICIENT_EVIDENCE (exit 2) exists
so an undersized compare (< `graded.promotion.min_cells`) is refused
*before* the first pairwise dollar — and it does NOT count as a REJECT for
`--new-evidence` purposes, because absence of evidence is not evidence
against; its remedy is a bigger matrix.

**The statistics are reported, not gated — and that's a defense, not a
dodge.** The verdict table prints the paired sign-test p-value and the 95%
confidence interval on the target-dimension delta. At the shipped matrix
size (8 cells, 2 held out), the sign test cannot reach p < 0.05 short of a
unanimous 6–0 sweep (p = 0.031) — a default p-gate would make the shipped
configuration *structurally unpromotable* while displaying the ceremony of
rigor. The honest alternative implemented: print both numbers so small-n is
visible instead of implied, gate on the both-order win pattern (which is
robust at small n), and offer `graded.promotion.max_sign_p` as an opt-in
for teams running matrices big enough to clear it. If you need to detect
small effects, scale `graded.max_cells_per_suite`; reinterpreting p-values
at n=6 is the thing the printout exists to prevent.

## 8. Holdout and confirm

Optimization overfits — that's what optimizing *is*. Iterating compare →
verdict against the same cells selects for candidates that win *those
cells*, and nothing in the verdict machinery can detect it, because the
contamination is in the process, not the judging.

The defense is data hygiene copied from ML practice: `graded.holdout`
(default 25%) of matrix cells are reserved at generation time and excluded
from every baseline and compare. They are spent exactly once per promotion,
by `confirm`, after `promote`. The reservation is a deterministic hash
ranking of cell ids (`src/science/gen_matrix.py`) — not hand-picked, not
re-rollable; regenerating the matrix reproduces the same split, so you
cannot shop for an easy holdout.

Confirm's contract is deliberately brutal: **any** finding on the holdout
reverts the promotion — copy the prior production prompt back, return to
compare. There is no "but the aggregate still looks fine" path, because the
holdout exists precisely for the case where the aggregate lied. A failed
confirmation is never argued with; a promotion that survives it has now won
on cells the optimization never saw. Pinned regression cases (§10) ride
along even in confirm's holdout-only pass — a fixed failure must never
return, least of all at promotion time.

The cost accounting is honest about the tradeoff: holdout cells are matrix
cells you paid to generate but don't use for iteration. That's the price of
being able to believe your own promotions, and at default sizes it is two
cells.

## 9. The cost model

The governance stack, from outermost to innermost:

1. **Pre-run estimate vs ceiling** — before the lock, the run dir, or any
   call. The estimate is *measured*: per-trial generation and per-call judge
   rates from the newest archived run touching the suite
   (`measured_estimates` in `src/utils/cost_tracker.py`), cache-aware
   (expected replays priced at $0 — the static estimator's 2× over-predict
   caused false aborts in the JS kit). `governance.estimate` is only the
   no-history fallback, and every printed estimate states its basis so an
   operator can tell a measured number from a guess (incident #6).
2. **Ceilings, layered** — the run ceiling (`governance.max_run_cost_usd`,
   default $1.00; `--max-cost` overrides explicitly and is recorded), an
   optional per-suite `max_run_cost_usd` (effective ceiling = min), optional
   per-role ceilings (`governance.max_cost_usd` for generation/judge/
   dataset), and optional rolling windows (`governance.max_daily_usd`,
   `governance.max_weekly_usd`) over the durable cross-run ledger — rolling
   24h/168h, not calendar days, because timestamp math is timezone-proof
   and testable. The windows are the second line of defense: a run-level
   cap does not stop fifty capped runs.
3. **The watchdog** — polls measured spend and status events during the run;
   kills the process group on ceiling breach, role-cap breach, or the
   failure breaker (consecutive errors / error rate, truncations included).
   A 100%-failing run must never burn to the dollar cap on error handling
   alone (incident #7).
4. **Honest accounting after** — the manifest gets measured spend; the
   rollup ledger gets one chained line per spending command; the receipt
   line prints spent / paid calls / cache replays avoided (measured, from
   `cache_hit` entries) / dollars refused at the gates (from
   `blocked-spend.jsonl`). The refusals are metered on purpose: a gate
   whose value is invisible gets deleted by the next optimizer.

Pricing rules: USD per MTok per model in `config/eval_config.yaml`;
unknown models price at the **most expensive** row so mispricing can only
over-count (the safe failure — it trips ceilings early); `pricing.last_verified`
is stamped and `doctor` warns past 180 days. Http-provider generation is
unmeterable by construction and is ledgered as `cost_unknown` at $0 with a
loud judge-spend-only alert — pretending to a number would be worse than
declaring its absence.

## 10. Caching, idempotency, ledgers

Three different mechanisms that share one principle: **an event that
happened must leave a durable, tamper-evident trace; an event that didn't
happen must not be billable.**

**Caching** is promptfoo's, deliberately (§3). The engine's contributions
are the cache-correctness lessons: keys must include every
output-determining parameter (incident #3 — a hollow output cached under a
key that ignored the failure), workspaces pin content-derived mtimes so the
mtime-hashing fingerprint changes iff content changes, the child env is a
deterministic allowlist so shell noise can't poison keys, and each trial
carries its own cache salt so k samples are k samples. Replays are metered
as `cache_hit` at $0 with the avoided cost recorded — savings are measured,
not folklore. `cache verify` codifies the hollow-entry heuristics.

**Idempotency** appears wherever a repeat could otherwise masquerade as new
evidence: verdicts (§7), the spend rollup (one line per runId, replays
don't double-count), state recording (a subset run records `smoked`, never
a baseline). The common shape: identity is content-derived (SHAs, run ids),
and repeats need explicit human reasons.

**Ledgers** are append-only JSONL, one per concern: per-run `spend.jsonl`
(every spending event, plus $0 entries for alerts, cache hits, and
cost_unknown), durable `outputs/history/spend-ledger.jsonl` (cross-run
rollup feeding the rolling windows), `blocked-spend.jsonl` (what the gates
refused), `spot-checks.jsonl` (human labels), verdicts. The durable ones are
**hash-chained** (`src/utils/chain.py`): each line carries the previous
line's hash, so edits, insertions, and deletions break every hash after
them, and `history verify` walks the chains. The legacy unchained prefix is
anchored by a file-bytes hash rather than grandfathered invisibly. This is
tamper-*evidence*, not tamper-*proofing* — an attacker with the repo can
rebuild chains — but the threat model is future-you rationalizing an edit,
not an adversary, and evidence is what that threat model needs.

## 11. Bare-ship and the init design

The repo ships **bare**: `src/` is the engine, and the project layer
(prompts, datasets, rubrics, fixtures) is empty scaffolding. The rejected
alternative — shipping demo suites pre-wired — fails two ways: demo content
gets copied into real projects as cargo cult (demo rubric anchors grading
real support tickets), and the first-run experience becomes "delete someone
else's project before starting yours". The worked example exists instead as
`examples/support-reply/`, adopted *through the normal path* by
`init --example support-reply` — onboarding runs the same machinery users
will run, so the example can never rot separately from the product.

`init` itself is built around one contract: **valid on arrival**. A
scaffolded suite passes `validate` immediately, with every unreal value
carrying the loud `EVAL-INIT-PLACEHOLDER` marker. The marker is the
teaching device — `validate` warns while any remains, `round` shows the
suite as scaffolded, and `calibrate` refuses placeholder goldens, so
nothing paid can build on scaffold content by accident, while the free
pipeline is provable end-to-end from minute one. Each placeholder you
replace upgrades one link of the proof.

Detection (bare prompt / wrapped / agent / bundle) is heuristic with
exactly one permitted question (bare vs agent), answerable
non-interactively (`--as`, `--yes`, or an `input.yaml` manifest) — an init
that interrogates you is an init people script around. Config edits
preserve comments, back up the file, and can be printed instead of applied
(`--print-config`). Reruns are idempotent; an existing suite id with
different content is a hard error, never an overwrite. And `--rubric <kind>`
is explicit rather than auto-detected on principle: a silently wrong task
kind would poison calibration semantics, and the failure would surface
weeks later as "the judge feels off".

## 12. Graft and snapshots

Successor wiping (§5) has a false-positive mode: a config-only edit —
reordering keys, adding a comment, tuning a ceiling — changes the config
sha, which stales `validated`, which wipes everything downstream. The
content the stages actually measured didn't change; re-earning them would
spend real dollars to reprove known facts.

`graft` is the deliberately narrow answer: it restores wiped entries from
the automatic snapshot **only when their own content fingerprints still
match current disk** — prompt sha, rubric sha, matrix sha, judge identity.
Anything whose inputs actually moved is refused with the reason printed.
Restored entries are stamped `restored_from`, so a grafted record never
impersonates a fresh one. The design constraint that shaped it: recovery
must not become a bypass. You cannot graft your way past a real change,
because the fingerprint match *is* the proof no real change happened.

Snapshots fire **once per process** before the first wipe (incident #16 —
per-wipe snapshots overwrote each other six times and preserved one suite
of six), rotate under `outputs/.state-snapshots/` (keep 10), and are
written atomically (`src/utils/fsatomic.py` — temp file + rename, because a
torn `.state.json` reads back as "nothing was ever earned"). Behind both
sits the real safety net: `graft --from-history` rebuilds state from the
durable records in `outputs/history/`, applying the same
fingerprint-must-match rule. Working state is a cache; history is the
truth (§5).

## 13. The dashboard's read-only doctrine

The dashboard (`src/dashboard/`) renders spend, pipeline state, live-run
progress, history trends, and judge evidence. Its constitution has three
articles, all enforced by construction rather than policy:

1. **Read-only** — the HTTP handler serves GET only. No button on the
   dashboard can spend money, record a stage, or mutate state. The moment a
   dashboard can act, it becomes an unaudited entrypoint competing with the
   gated CLI — incident #14 was exactly the class "spending paths that
   don't self-register", and a clickable "re-run" button would reintroduce
   it with a friendlier face.
2. **Localhost-only** — bound to 127.0.0.1, non-negotiably. Run artifacts
   contain your prompt text, probe content, and judge rationales; a
   convenience bind-all flag would be a data-exfiltration feature with a
   UX. Teams that need shared visibility should share `run.json` artifacts,
   not open the port.
3. **Artifacts are the interface** — the dashboard re-reads run files fresh
   on every poll and holds no state of its own. It can therefore never
   disagree with `report`, never needs migration, and is safe to leave open
   beside a running eval. Per-case output browsing stays with
   `promptfoo view`, which already does it well (§3: don't rebuild the
   engine's own instruments).

## 14. The `src` package-name wart

The engine's Python package is named `src`. Every import reads
`from src import config`; every assert reference reads
`file://src/...:function`. By packaging convention this is wrong, and it is
kept deliberately. The defense:

- **The name is load-bearing in recorded history.** Assert references,
  prompt-source paths, and fingerprinted file paths in years of durable
  records all begin with `src/`. Renaming the package would either stale
  every recorded stage and break `graft --from-history` (the fingerprints
  embed paths), or require a rewrite shim over the audit trail — and an
  audit trail you rewrite is not an audit trail.
- **The usual harms don't apply here.** The `src` name is harmful in
  *installed* packages (it shadows every other project's `src`). This
  engine is never installed (§18) — it runs from a checkout with the repo
  root on `sys.path`. Within a checkout, the name is unambiguous.
- **The cost of the wart is one raised eyebrow per new reader** — which this
  section exists to answer. The cost of fixing it is a breaking migration
  of the thing the project exists to preserve. Warts you can explain are
  cheaper than surgeries you can't justify.

If this project were being started today, the package would be named
`prompt_eval` on day one. It wasn't, and day 400 is the wrong day.

## 15. The zero-spend test architecture

The test suite proves a *paid* pipeline without spending a cent, under a
standing zero-spend rule. The architecture that makes this credible rather
than hopeful:

- **Structural impossibility in CI.** The workflow blanks
  `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` explicitly — not merely omits
  them — so no code path, mocked or not, can reach a billable endpoint.
- **The mock lattice.** Each paid dependency has a purpose-built mock at
  its natural seam: `MOCK_JUDGE` (offline validate's llm-rubric),
  `MOCK_GRADED_JUDGE` (canned graded-judge replies — `good`, `bad`,
  `malformed`, `malformed-once` for the re-ask path, and `band`, which
  scores each golden from its own label so offline calibration can go
  green while proving wiring and band arithmetic, never judge quality),
  `MOCK_PAIRWISE`, `MOCK_DATASET`, and the `mock_golden` provider that
  replays golden bundles under `EVAL_OFFLINE=1` via
  `promptfooconfig.offline.yaml`. Every mock announces itself: a mocked
  judge records `judge_model: mock:*`, which can never satisfy a paid
  gate — mocks prove plumbing, and the records they leave say so.
- **Synthetic projects, not repo state.** Tests build throwaway projects
  (`tests/conftest.py`) and point `EVAL_CONFIG` at them; the shipped repo
  stays bare, and tests can't depend on — or corrupt — real project
  content. Run-scoped env is scrubbed between tests for the same reason.
- **Fabricated artifacts for artifact-readers.** Commands that read run
  dirs (`spot-check`, `report`, `verdict`, `history`) are tested against
  hand-built run directories — the reader's contract is the file format,
  so the file format is what tests construct.
- **The honest limit, stated:** mocks cannot prove judge quality, provider
  behavior, or real latency/cost. That residue is exactly what `preflight`
  and the live calibration are for, and the WALKTHROUGH sizes them before
  a dollar moves. The zero-spend rule buys unlimited iteration on
  everything else.

## 16. Docs-drift philosophy

Incident #13: the method doc described paths that didn't exist, env vars
nothing read, and a config header claiming "no API keys" atop the file that
loaded the key. Documentation had become *actively worse than nothing* —
it answered questions wrongly with confidence.

The enforced rule (`tests/test_docs_drift.py`): every backticked path,
`EVAL_*` env var, CLI subcommand, and command-line flag in the scanned docs
(README, RUNBOOK, every page under `docs/`, the example WALKTHROUGH) must
exist in the code, and every CLI subcommand must be documented in the README. The
mechanism is deliberately dumb — regex over backticks — because dumb is
what survives: it needs no annotations, no doc build, and it fails with
the exact token that lied.

The philosophy underneath: **docs are code's dependents, and dependencies
that can break silently, will.** Three corollaries shape the repo. New
subcommands ship with their README row in the same commit (the test makes
half-shipped CLI surface impossible). Runtime-created paths are a declared
allowlist (`RUNTIME_PATHS`), so "this file will exist after a run" is an
explicit claim rather than a loophole. And `CHANGELOG.md` is deliberately
*excluded* from scanning — history must be allowed to name deleted things,
or honest history and passing tests become enemies.

What the drift check cannot catch — stale *reasoning*, outdated numbers in
prose, claims about behavior — is why verified-with-a-date claims carry
their date (§1) and why this handbook cites files instead of paraphrasing
them.

## 17. Known gaps and accepted tradeoffs

Recorded so nobody rediscovers them as bugs, and nobody "fixes" them
without meeting the reasoning that put them there:

- **The default matrix is statistically small.** 8 cells (2 held out)
  cannot clear p < 0.05 without a unanimous sweep; the detectable-effect
  floor is high. Accepted because matrix size is a *cost* decision the
  user owns (`graded.max_cells_per_suite`), and the verdict prints the
  small-n statistics rather than hiding them (§7).
- **Adaptive-k never escalates automatically.** Screen suites run k=1 and a
  flagged result warrants a manual full-k re-run. An auto-escalation path
  would be a spending decision taken by code; the engine's constitution is
  that spending decisions print their price and wait for a human.
- **The cross-family judge path has never taken a live call** — wired,
  tested against mocks, and declared in the RUNBOOK and
  [providers.md](providers.md). First live use should start with a small
  calibration.
- **Multi-turn is simulation** (`messages:` renders a labeled transcript
  into a single prompt); real turns need an http suite. The agent-sdk
  provider takes one free-text prompt — the honest options were simulation
  flagged as such, or nothing.
- **Http-provider generation is unmetered** (`cost_unknown` at $0, ceiling
  governs judge spend only). The alternative — asking users to declare a
  price for someone else's endpoint — would put a guessed number where the
  post-mortem taught us guessed numbers do damage (§2, pattern 2).
- **Judge-prompt text is not fingerprinted.** The rubric is; the
  surrounding instruction scaffold in `src/utils/rubric.py` is not — a
  scaffold edit silently ages calibrations. Accepted with a batching
  discipline (judge-prompt edits land together, with a recalibrate note)
  because fingerprinting the scaffold would stale every suite on every
  engine upgrade, making upgrades themselves expensive.
- **`spot-check` labels are binary** (pass/fail per dimension vs the
  threshold), coarser than the judge's 1–5. Chosen because binary human
  judgments are fast and reliable; five-point human labeling triples the
  effort for agreement arithmetic the threshold comparison doesn't need.
- **The Python API is thin and not re-entrant** (`src/api.py` — the runner
  mutates env and holds a run lock). Accepted: the alternative was a
  parallel programmatic pipeline with its own gate bugs. `run.json` is the
  integration contract; the facade is a convenience.
- **State history's disaster recovery has one manual seam**: if both state
  file and snapshots are lost *and* history records were pruned, stages
  must be re-earned. Judged acceptable — the scenario requires deleting
  tracked files, and re-earning is expensive but correct.

## 18. Release and distribution decisions

- **Clone-or-template, deliberately not PyPI.** Three reasons, any one
  sufficient: the top-level package name (§14) would shadow user projects
  the moment it's installed; the CLI is anchored to a checkout (repo-local
  promptfoo configs and `node_modules` — an installed console script would
  be non-functional by design, not by accident); and a closed,
  bug-fixes-only project (README → Status) should not take on a release
  channel whose whole value is frequent updates. Packaging metadata is
  still kept accurate and `uv build` is smoke-tested at release, so the
  *option* stays cheap if a future maintainer re-decides.
- **Three version strings, one sync test.** The version lives in
  `pyproject.toml`, `package.json`, and `src/config.py` (ENGINE_VERSION);
  `tests/test_version_sync.py` asserts they match. Single-sourcing was
  rejected: each location is read by a different toolchain at a different
  time (pip metadata, npm, import time), and the indirections needed to
  unify them cost more than a small test. (An `engine.version` key in the
  config template was a fourth copy read by no code — deleted.)
- **CI is spend-impossible, and `doctor` is deliberately not in CI** — it
  hard-fails on a missing API key *by design* (that's its job on a
  developer machine), and CI structurally has no key. The example-smoke
  job covers what CI can honestly prove: the whole free path, on real
  example content, at $0.
- **promptfoo stays exact-pinned**; upgrades are commits that re-run the
  full suite, never ranges resolved at install time.
- **Docker exists for reproduction, not distribution**: the image
  reproduces the checkout environment (node + uv + pinned deps);
  `.dockerignore` excludes `.env` so a live key can never bake into a
  layer. No registry publishing — an image nobody rebuilds is a supply
  chain with extra steps.
- **Release ritual**: version bump across the four spots (sync test
  enforces), full test pass, CI green, local docker build check, annotated
  tag. The CHANGELOG is reconstructed honestly (it says so) — it was
  written at v1.0.0, not maintained along the way, and pretending
  otherwise would be the docs-drift sin with a date attached.

## 19. Defending the design — the FAQ

Twelve challenges this project actually receives, each with the answer and
the evidence to check.

**Q1. "Isn't this just promptfoo with extra steps?"**
Yes — and the steps are the product. Every run *is* `promptfoo eval`; the
audit (LESSONS-LEARNED §4) deleted ~1,100 lines that duplicated it. What
remains is exactly what a dated market survey found nowhere: measured
pre-spend gates with a mid-run kill switch, calibration that gates scoring,
blinded idempotent verdicts, and the state machine. Evidence: delete the
Python layer and re-run the post-mortem's incidents; promptfoo alone stops
none of the sixteen. See §3, [comparison.md](comparison.md).

**Q2. "Six cells prove nothing. Your n is a joke."**
Six cells prove less than sixty — and the engine says so out loud instead
of hiding it: the verdict prints the sign-test p and the CI, and its
*gate* uses both-order pairwise wins, which is meaningful at small n where
p-thresholds are theater (§7). Matrix size is a knob
(`graded.max_cells_per_suite`); the default is a cost floor, not a
statistical claim. What would actually be a joke is n=6 with a p<0.05
badge on it.

**Q3. "An LLM judging an LLM is circular."**
Unanchored, yes. Three anchors break the circle: the judge is a different
model (hard error otherwise, §4), it must land human-labeled goldens in
band before grading anything (§6), and its live agreement with a human is
tracked (`spot-check`, threshold 0.9). The judge is an instrument —
calibrated before use, spot-checked in use, recalibrated when anything it
was calibrated against moves.

**Q4. "Why a temperature-0 judge? Sampling would average out bias."**
Sampling averages out *noise*, at k× the price, and adds variance to every
number downstream; it does nothing for *bias* (a judge that flatters
verbose outputs flatters them at every temperature). Determinism makes the
remaining bias measurable and repeatable — which is what calibration and
position-swapping then attack directly. The temperature is fingerprinted,
so a team that disagrees can change it and recalibrate — visibly. (§4, §6)

**Q5. "Why not gate promotion on p < 0.05 like real science?"**
Because at the shipped matrix size that gate is either dishonest or
prohibitive: unreachable short of a unanimous sweep, so it would quietly
convert "promote on evidence" into "never promote" while wearing rigor's
clothes. Real science reports its power; so does the verdict table. Teams
with bigger matrices can opt in via `graded.promotion.max_sign_p`. (§7)

**Q6. "Why isn't this on PyPI?"**
Because installing it would break it, and the package name would break
*you* (§14, §18). It's a repo you clone or template — which is also the
distribution model of every project layer built on it, since your prompts,
rubrics, and goldens were never going to come from a wheel anyway.

**Q7. "Why does a comment edit in the config wipe my pipeline?"**
Because the state machine fingerprints the config file, and it cannot know
which bytes are load-bearing without parsing intent. The recovery is
`graft`: free, immediate, and restoring only entries whose *own* inputs
(prompt, rubric, matrix, judge) provably didn't change — recovery without
a bypass (§12). The alternative — semantic config diffing — is a
correctness bet the audit trail shouldn't ride on.

**Q8. "Why can't the engine draft my golden fixtures? It drafts test cases."**
Because the two artifacts have opposite trust directions. Drafted *cases*
are inputs — reviewed by you, then proven by running (§4). Goldens are the
**measuring standard**: calibration compares the judge to them. A golden
drafted by the judge's own family is the standard calibrated to the
instrument — GREEN by construction, meaning nothing. Cases: cheap model,
human review. Goldens: human curation, full stop. (§6,
[calibration.md](calibration.md))

**Q9. "Why is there no scheduler / cron / daemon for monitor?"**
`monitor` is a spending command, and the engine's constitution is that
spending is a human act with a printed price (§17). Cron already exists,
and the shipped PR-workflow example shows the CI shape. Owning a scheduler means owning
credential storage, missed-tick semantics, and unattended spend — three
liabilities to duplicate a tool every machine ships with.

**Q10. "The dashboard can't even re-run a failed suite. Why so useless?"**
That button is incident #14 with better UX (§13). Every mutation path goes
through the CLI where gates, locks, ledgers, and recorded `--force`s live.
The dashboard shows you the truth; acting on it costs one command that will
tell you its price first.

**Q11. "Why do tests never hit the real API? Mocked tests prove nothing."**
Mocked tests prove everything except three things — judge quality,
provider behavior, real prices — and the engine routes exactly those three
to purpose-built paid steps (`preflight`, live `calibrate`, the sized
walkthrough) instead of smearing them across CI (§15). A test suite that
spends money rots into a test suite nobody runs; this one runs on every
commit with the keys structurally blanked.

**Q12. "This is a lot of ceremony for editing a prompt."**
The ceremony prices in at roughly six commands per promotion — and it
exists because the alternative was measured, in dollars: $130.15, a
re-rolled REJECT, a poisoned cache, and a promotion nobody could defend
(§2). Ship a prompt change without evidence and you've spent less only if
you never count what the change does in production. For changes below the
ceremony's threshold, `baseline` and the free `validate` still exist — the
full protocol is for changes you intend to *defend*.

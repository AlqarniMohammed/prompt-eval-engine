# Concepts & glossary

One grep-able page. Each term: what it is, why it exists, and the file that
defines it. Alphabetical.

**Adaptive-k (screening)** — listed suites run k=1 instead of the full
`graded.repeat` to save spend on suites already proven ceiling-pinned. Screen
only: nothing escalates automatically; a flagged screen result warrants a
manual full-k re-run. Defined in `src/loaders/pf_tests.py` (`_graded_repeat`).

**Band (calibration band)** — the score range a golden fixture must land in
for the judge to be trusted: every `pass*.txt` sample ≥ `pass_min` on every
dimension, every `fail*.txt` ≤ `fail_max` on its declared target dimension,
every `mid*.txt` mean strictly between the two. Bands are the falsifiable
claim behind "the judge works". Defined in `src/science/calibrate.py`;
rationale in [calibration.md](calibration.md).

**Baseline (baselined stage)** — the production prompt's dimension scores on
the full graded matrix; every later comparison and drift check is measured
against it. Earned by a full `graded` run, recorded in the state machine.
Defined in `src/runner.py` (`_record_run_stages`).

**Blinded pairwise verdict** — the promotion decision: for each compared cell
the judge sees both outputs labeled only A/B, twice with positions swapped;
a cell counts as a win only when both orders agree. Kills self-preference
and position bias. Defined in `src/science/pairwise.py`.

**Breaker (failure breaker)** — the watchdog rule that kills a live run after
consecutive case errors or a high error rate (truncated outputs count): a
100%-failing run must never burn to the dollar ceiling. Defined in
`src/utils/proc.py` (`_breaker_tripped`).

**Bundle (envelope)** — the plain-text format every graded output takes:
an `===== STDOUT =====` section plus one `===== FILE: path =====` section per
file the agent created or changed. Asserts and judges read this one shape
whether the output came live or from a golden. Defined in
`src/utils/bundle.py`.

**Calibration** — proving the judge against golden fixtures before it grades
anything real: k samples per golden, ALL samples in band, recorded with the
rubric sha, judge model, temperature, and provider-resolved model id. A
graded run is refused without a green, current calibration. Defined in
`src/science/calibrate.py`; theory in [calibration.md](calibration.md).

**Case / probe** — one test: the user message (`vars.probe`, or the final
user turn of a simulated `messages:` transcript) fed to the prompt under
test, plus its contracts and golden reference. Loaded by
`src/utils/dataset_loader.py`.

**Ceiling** — the hard dollar limit a run must fit under, checked against a
measured estimate BEFORE the first call and enforced mid-run by the watchdog.
Effective ceiling = min(run ceiling, suite ceiling), `--max-cost` overrides
explicitly and is recorded. Defined in `src/runner.py` (`_run_live`) and
`src/utils/proc.py`.

**Cell** — one point of the graded matrix: a combination of axis values
(e.g. tone=angry × issue=late) with its probe. Cells are generated from a
spec by `matrix`, never hand-shuffled. Defined in
`src/science/gen_matrix.py`.

**Confirm (confirmed stage)** — the post-promotion run on holdout cells the
optimization never saw. A clean pass records `confirmed`; any finding means
the promotion is reverted, not argued with. Defined in `src/runner.py`.

**Contract** — a deterministic, free check on an output (format present,
forbidden content absent). Contracts run on every tier and catch what a
judge can miss; reusable ones ship in `src/contracts_lib.py`.

**Cost estimate (measured basis)** — pre-run cost prediction learned from the
newest archived run touching the suite (per-trial generation rate, per-call
judge rate); static config numbers are only the no-history fallback.
Defined in `src/utils/cost_tracker.py` (`measured_estimates`).

**Dimension** — one named 1–5 axis of a rubric (e.g. `resolution`), with all
five anchors written out. The judge scores every dimension with evidence and
reasoning; pass = every dimension ≥ `graded.pass_threshold`. Parsed by
`src/utils/rubric.py`.

**Dry run** — `--dry-run` on every paid command: run the state gates, print
the full cost breakdown (per-role calls, unit costs, basis, total vs
ceiling), then exit before the lock, the run dir, or any call. Defined in
`src/runner.py` (`_dry_run_report`).

**Fingerprint (staleness)** — content SHAs of everything a recorded stage
depended on (prompt text, rubric, config, matrices, regression set). A stage
whose fingerprint no longer matches disk is stale and must be re-earned;
`why` names the exact stale key. Defined in `src/state.py`.

**Golden fixture** — a known-good (`pass*.txt`), known-bad (`fail*.txt`), or
deliberately mediocre (`mid*.txt`) output bundle, written or curated by a
human. Goldens power the free offline validate sweeps and judge calibration.
Read by `src/providers/mock_golden.py`; policy in [calibration.md](calibration.md).

**Graft** — restoring wiped pipeline stages whose content fingerprints still
match the automatic snapshot (or, with `--from-history`, rebuilding state
from durable history records). It cannot smuggle stale results: anything
whose fingerprint moved is refused with the reason printed. Defined in
`src/state.py`.

**History (hash-chained ledgers)** — durable append-only records under
`outputs/history/` (verdicts, spend rollup, blocked spend, spot-checks),
each line carrying a hash of the previous line so edits, inserts, and
deletions are tamper-evident (`history verify`). Defined in
`src/utils/chain.py`.

**Holdout** — the fraction of matrix cells (deterministically hash-ranked,
not hand-picked) excluded from every optimization run and spent only at
`confirm`. Defined in `src/science/gen_matrix.py`.

**HTTP provider suite** — a suite whose generation runs against your deployed
endpoint instead of the agent-sdk provider; judge and governance unchanged,
generation cost honestly `cost_unknown`. Defined in `src/runner.py`
(`_materialize_http_config`); wiring in [providers.md](providers.md).

**INSUFFICIENT_EVIDENCE** — the third verdict outcome (exit code 2): the
compare had fewer cells than `graded.promotion.min_cells`, so no pairwise
dollar is spent and the remedy is a bigger matrix — distinct from REJECT,
which is evidence *against*. Defined in `src/science/pairwise.py`.

**Judge** — the model that grades outputs; must differ from the generation
model (hard error) and is valid only behind green calibration. Dispatched by
model prefix in `src/utils/model_client.py`, driven by
`src/evaluators/llm_judge.py`.

**Ledger (spend.jsonl)** — the per-run append-only record of every spending
event (generation, judge, dataset, cache_hit at $0, cost_unknown at $0,
alerts). Manifests, the watchdog, and estimates read measured entries, never
guesses. Defined in `src/utils/cost_tracker.py`.

**Manifest** — the run's reproducibility contract, written before the first
call and updated after: content SHAs, effective models and caps, env
overrides used, forced gates, ceilings, spend. Defined in `src/runner.py`
(`_build_manifest`).

**Monitor (canary)** — an on-demand drift check: a tiny fixed hash-ranked
subset of graded cells re-run and compared to the baselined dimension means;
exit 1 on drift. No scheduler on purpose. Defined in `src/runner.py`
(`cmd_monitor`).

**Pin (regression suite)** — copying a once-failed cell verbatim into
`datasets/regression/`; it then rides along in every graded-tier run and any
recurrence fails the run loudly regardless of aggregates. Defined in
`src/science/pin.py`.

**Preflight** — one real generation case plus one judge call on the exact
configured models and caps, required before campaigns: prove the REAL
configuration, not just its cheapest half. Defined in `src/runner.py`.

**Simulated multi-turn** — `messages:` on a case rendered as a labeled
transcript block inside the single prompt; honest scope, flagged
`simulatedMultiTurn` in the manifest. Real turns need an HTTP suite. Defined
in `src/utils/dataset_loader.py`.

**Spot-check** — a human labeling pass over a finished run's judged cells,
blind first (judge scores revealed after), chained into history; rolling
human–judge agreement below the threshold warns everywhere trust is spent.
Defined in `src/science/spot_check.py`.

**State machine** — the six recorded stages (validated → calibrated →
baselined → compared → promoted → confirmed) with fingerprint staleness and
successor wiping; `--force` bypasses are recorded, never silent. Defined in
`src/state.py`.

**Suite** — one prompt under test plus its dataset, contracts, rubric,
goldens, and spec; declared in `config/eval_config.yaml`, identified by an
opaque id.

**Taxonomy (failure tags)** — the controlled vocabulary (`graded.taxonomy`)
judges and contracts use to tag the top issue (`[format]`, `[tone]`, ...), so
failures aggregate across a run instead of drowning in prose. Defined in
`src/utils/rubric.py`.

**Trial (k)** — one repeated execution of a cell; graded cells run k times
because variance is data, not noise. Each trial gets its own workspace and
cache identity. Defined in `src/loaders/pf_tests.py`.

**Value receipt** — the honest accounting line after every run and in
`round`: dollars spent, calls made, cache replays avoided (measured), and
dollars refused at the gates. Defined in `src/utils/cost_tracker.py`
(`savings_totals`).

**Watchdog** — the poll loop around the promptfoo child: measured spend vs
ceiling, role caps, breaker state, progress heartbeat; kills the process
group the moment a limit is crossed. Defined in `src/utils/proc.py`.

**Workspace (fixture)** — the fresh per-trial copy of a suite's fixture
directory an agent prompt works in; files it creates or edits become FILE
sections of the judged bundle. Defined in `src/loaders/pf_tests.py`
(`_materialize_workspace`).

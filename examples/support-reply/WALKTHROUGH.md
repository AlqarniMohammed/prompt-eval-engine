# Walkthrough: the support-reply example, end to end

Two halves, honestly separated. **Part 1 costs $0** and the console output
below is verbatim from a real run (only timestamps and run-ids will differ on
your machine — it runs as the `example-smoke` CI job on every commit).
**Part 2 spends real API credit**, so it shows the *shape* of each step and
how to size it with `--dry-run` first — no fabricated transcripts.

Prerequisites: `npm ci` and `uv sync` have run. Nothing in Part 1 reads an
API key.

## Part 1 — the free half (verbatim)

### 1. Ask the engine where you are

```bash
uv run prompt-eval round
```

```text
No suites declared yet. Drop a prompt (or a folder) into input/ and run `prompt-eval init` — it detects what you dropped, scaffolds the rest, and wires the suite (input/README.md has the full menu). Or take the guided tour on real content: prompt-eval init --example support-reply
```

### 2. Adopt the example

```bash
uv run prompt-eval init --example support-reply
```

The bundle is staged into `input/` and adopted through the exact same path
your own dropped prompts take — the example is not special-cased:

```text
staged examples/support-reply/bundle → input/support-reply
────────────────────────────────────────────────────────────────────────
init      suite support-reply (bundle) registered
────────────────────────────────────────────────────────────────────────

FROM → TO
  support-reply.md             → prompts/production/support-reply.md
  cases.yaml                   → datasets/support-reply.yaml
  rubric.md                    → judges/support-reply.md
  spec.yaml                    → datasets/graded-specs/support-reply.yaml
  contracts.py                 → project/contracts.py
  fail1.txt                    → fixtures/golden/support-reply/fail1.txt
  fail2.txt                    → fixtures/golden/support-reply/fail2.txt
  fail3.txt                    → fixtures/golden/support-reply/fail3.txt
  pass1.txt                    → fixtures/golden/support-reply/pass1.txt
  pass2.txt                    → fixtures/golden/support-reply/pass2.txt
  pass3.txt                    → fixtures/golden/support-reply/pass3.txt
  (scaffolded)                 → datasets/graded/support-reply.yaml

CONFIG    declare suite support-reply · set project.asserts_file = project/contracts.py
MATRIX    4 graded cells generated
```

(followed by the per-file "WHAT EACH FILE PROVES" table and three numbered
next steps — read them, they are the engine teaching its own pipeline. A
bundle ships real content, so there are no placeholders to replace: step 1
is the free `validate` below, and everything after it costs money.)

### 3. Prove the suite without a model call

```bash
uv run prompt-eval validate
```

```text
== 1/4 declaration + fixture checks ==
  all declarations resolve

== 2/4 golden PASS set (every variant must satisfy every assertion) ==
-- pass variant 1/3
12 cases checked (expect-all-pass): 12 as expected, 0 not
-- pass variant 2/3
12 cases checked (expect-all-pass): 12 as expected, 0 not
-- pass variant 3/3
12 cases checked (expect-all-pass): 12 as expected, 0 not

== 3/4 golden FAIL set (every variant must be caught) ==
-- fail variant 1/3
12 cases checked (expect-all-fail): 12 as expected, 0 not
-- fail variant 2/3
12 cases checked (expect-all-fail): 12 as expected, 0 not
-- fail variant 3/3
12 cases checked (expect-all-fail): 12 as expected, 0 not

== 4/4 rubric wiring (MOCK_JUDGE=fail must fail every rubric-bearing case) ==
-- pass variant 1/3
WARN  no rubric-bearing cases in this suite — this sweep proved nothing (binary contracts only; the graded tier is where the rubric runs)
-- pass variant 2/3
WARN  no rubric-bearing cases in this suite — this sweep proved nothing (binary contracts only; the graded tier is where the rubric runs)
-- pass variant 3/3
WARN  no rubric-bearing cases in this suite — this sweep proved nothing (binary contracts only; the graded tier is where the rubric runs)

OFFLINE VALIDATION GREEN — the suite is proven without a single model call.
Next: prompt-eval calibrate --suite support-reply
```

What just got proven, for free: every declaration resolves, all three PASS
goldens clear all four contracts, and all three FAIL goldens are *caught* by
at least one contract — your checks can actually detect bad output. Note the
honest WARN on step 4/4: this bundle's dataset carries binary contracts only,
so the rubric-wiring sweep has nothing to check — the rubric is exercised by
the graded tier (and its calibration, next), not here.

### 4. Prove the calibration wiring (mock judge, still $0)

```bash
MOCK_GRADED_JUDGE=band uv run prompt-eval calibrate --suite support-reply
```

```text
== calibrating judge for support-reply (k=3, pass>=4, fail<=2) ==
  support-reply/pass1.txt [pass]: resolution 5..5 · accuracy 5..5 · empathy_tone 5..5 · scope_safety 5..5
  support-reply/pass2.txt [pass]: resolution 5..5 · accuracy 5..5 · empathy_tone 5..5 · scope_safety 5..5
  support-reply/pass3.txt [pass]: resolution 5..5 · accuracy 5..5 · empathy_tone 5..5 · scope_safety 5..5
  support-reply/fail1.txt [fail]: resolution 2..2 · accuracy 2..2 · empathy_tone 2..2 · scope_safety 2..2
  support-reply/fail2.txt [fail]: resolution 2..2 · accuracy 2..2 · empathy_tone 2..2 · scope_safety 2..2
  support-reply/fail3.txt [fail]: resolution 2..2 · accuracy 2..2 · empathy_tone 2..2 · scope_safety 2..2
  self-consistency: exact agreement 100% · mean spread 0.00 across 24 (golden × dimension) pairs
  record: outputs/history/calibration-support-reply-20260815-215921.json

CALIBRATION GREEN — every golden sample landed in band.
```

**Read this honestly**: the band mock scores each golden from its *own
label* (pass→5s, fail→2s, mid→3s). GREEN here proves the wiring and the band
arithmetic, **never judge quality**. The state record is stamped
`judge_model: mock:band`, so it can never unlock a paid graded run — only a
live `calibrate` earns that.

### 5. Where you stand now

```bash
uv run prompt-eval round
```

```text
support-reply                validated → calibrated (STALE)
                             next: prompt-eval calibrate --suite support-reply

preflight: NOT RUN — required before any campaign (prompt-eval preflight)

spend     $0.00 last 24h · $0.00 last 7d
```

`calibrated (STALE)` is the engine being honest with you: the record was
earned by `mock:band`, not the configured judge, so the paid pipeline treats
it as not-yet-earned and `next` points at the live `calibrate` — exactly
where Part 2 begins.

## Part 2 — the paid half (shape only, sized before spending)

No fabricated transcripts here: these steps call real models, so what you
will see depends on the models. What is fixed is the *sequence*, the *gates*,
and the fact that every step tells you its price before you pay it.

1. **Recalibrate live** — the band-mock record proves wiring only:
   `uv run prompt-eval calibrate --suite support-reply --dry-run` prints the
   judge-call count and cost basis; drop `--dry-run` to spend (~18 judge
   calls: 6 goldens × k=3). GO is every sample in band; NO-GO means fix the
   rubric anchors — never bend your labels.
2. **Preflight** (~$0.05): `uv run prompt-eval preflight` — one real
   generation + one judge smoke on the exact configured models and caps.
3. **Smoke, then baseline**: `uv run prompt-eval graded --suite support-reply
   --filter-first-n 2`, then the full
   `uv run prompt-eval graded --suite support-reply` (use `--dry-run` first —
   3 non-holdout cells × k=3 = 9 generation + 9 judge calls). The report
   lands in the run dir; dimension means become the baseline.
4. **Compare the candidate** — read `candidate/HYPOTHESIS.md` FIRST (the
   prediction is written before the scores exist), then:
   `cp examples/support-reply/candidate/support-reply.md prompts/candidates/`
   and `uv run prompt-eval compare --suite support-reply`.
5. **Verdict**: `uv run prompt-eval verdict --target empathy_tone` — blinded,
   position-swapped pairwise; PROMOTE needs both-order wins, REJECT and
   INSUFFICIENT_EVIDENCE are real outcomes.
6. **Promote + confirm**: `uv run prompt-eval promote --suite support-reply`,
   then `uv run prompt-eval confirm --suite support-reply` runs the held-out
   cell — a promotion that fails confirmation is reverted, not argued with.

Every paid command above accepts `--dry-run` (full cost breakdown, then
exits) and refuses to start above its ceiling without an explicit,
recorded `--max-cost`.

## Teardown (restore the bare repo)

Adoption writes real files into the project layer. To remove the example
completely:

```bash
git checkout -- config/eval_config.yaml
rm -rf prompts/production/support-reply.md datasets/support-reply.yaml \
       judges/support-reply.md datasets/graded-specs/support-reply.yaml \
       datasets/graded/support-reply.yaml project/ \
       fixtures/golden/support-reply/ input/support-reply/
rm -rf outputs/runs/validate-* outputs/runs/calibrate-* \
       outputs/.state.json outputs/.state.backup.json outputs/.state-snapshots/ \
       outputs/judge-evidence.jsonl config/eval_config.yaml.bak \
       outputs/history/calibration-support-reply-*.json \
       outputs/history/init-*.json
```

**Careful with the first line if you registered your own suites:** `git
checkout -- config/eval_config.yaml` reverts the whole file — every suite, not
just the example's entry. In that case delete the example's suite entry by
hand instead, then run `uv run prompt-eval graft` to restore your other
suites' wiped stages. `git status --short` empty is the proof the teardown is
complete (everything else the example touched is gitignored).

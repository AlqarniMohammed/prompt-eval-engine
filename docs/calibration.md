# Calibration: why the judge is trusted, and exactly how far

An LLM judge produces numbers that *look* precise whether or not they mean
anything. Calibration is the falsifiable claim that makes them mean
something: **before grading anything real, the judge must score outputs whose
quality a human already decided — and land every sample inside declared
bands.** The machinery lives in `src/science/calibrate.py`; the state machine
(`src/state.py`) refuses every graded run without a green, current
calibration record.

## The bands, and why these numbers

Defaults from `config/eval_config.yaml` (`graded.calibration`):

| Golden set | Requirement | Default |
|---|---|---|
| `pass*.txt` | every sample ≥ `pass_min` on **every** dimension | 4 |
| `fail*.txt` | every sample ≤ `fail_max` on its declared `failTarget` dimension | 2 |
| `mid*.txt` | every sample's mean strictly inside (`fail_max`+0.5 … `pass_min`−0.5) | 2.5–3.5 |

Why bands, not exact scores: two honest judges can defensibly disagree
between a 4 and a 5 on a good output — demanding exactness would make
calibration fail on judge temperament rather than judge competence. What a
trustworthy judge must NOT do is call a known-good output mediocre (a pass
scoring 3) or find redeeming precision in a known failure (a fail's target
dimension at 3+). The bands encode exactly that and nothing more.

Why `pass_min` is 4 and not 5: a 5 is "exemplary", and several genuinely
good outputs are 4s. Requiring 5s would push you to curate only flawless
goldens — which teaches you nothing about how the judge treats the good-but-
imperfect outputs your prompt mostly produces.

Why `fail_max` is 2 and not 1: a 1 is "unusable"; many realistic failures
are substantial-but-not-total (`- 2:` anchors). Requiring 1s would force
cartoonishly bad fixtures, and a judge calibrated on cartoons is uncalibrated
on reality.

Why the mid band exists: pass/fail alone lets a lazily bimodal judge (5s and
1s for everything) look calibrated. A `mid*.txt` fixture — a real mediocre
output — forces the judge to use the middle of its scale. Mid fixtures are
optional but recommended once you've seen your first genuinely so-so output.

**All k samples must land in band, not the average.** `samples: 3` per golden
means one wild sample fails calibration even when the mean looks fine —
variance in a temperature-0 grader is itself evidence something is off. The
free `consistency` block (exact agreement + mean spread across samples) is
computed from the same samples and printed so an unstable judge is visible
even when every sample scrapes into band.

## Fixture counts

The shipped example uses 3 pass + 3 fail; that is a floor, not a target.
Guidance:

- **Minimum**: 2 pass + 2 fail per golden set — below that, one fixture IS
  the calibration and a single curation mistake poisons it.
- **Better**: 3–5 of each plus 1–2 mid. Each additional fixture is another
  way the judge can be caught wrong, at k × fixture judge calls of one-time
  cost.
- Every `fail*.txt` should fail for a *different reason* (format break,
  policy invention, tone collapse…) so the bands test the rubric's breadth,
  not one failure mode three times.

## Adding fixtures — the one hard rule

**Goldens are never model-drafted.** A fixture the judge's family wrote is a
fixture the judge will flatter; calibrating against it is the engine grading
its own homework (the same reason `gen-fixtures` is a rejected feature —
see Non-goals in the [README](../README.md)). Sources that work:

1. Real outputs from live runs, hand-labeled: copy from a run dir, decide
   pass/fail/mid yourself, strip anything sensitive.
2. Hand-written or hand-mutilated outputs: take a good output and break it in
   the specific way you want caught (that is how the example's `fail1..3`
   were made).

Keep the `===== STDOUT =====` envelope; put the label in the filename
(`pass2.txt`, `fail3.txt`, `mid1.txt`); declare each fail's `failTarget`
dimension in the suite's graded spec. Then re-run `validate` (free — the
contracts must catch every new fail fixture) before spending on `calibrate`.

When a calibration misses the band, **fix the rubric anchors or the fixture —
never bend your labels toward the judge**. The miss report keeps the judge's
own evidence and reasoning (`evidenceOnMiss`) precisely so you can re-diagnose
without a paid re-run.

## The human side: spot-check agreement

Calibration proves the judge against fixtures; the rolling human agreement
from `spot-check` proves it against *live* outputs over time (blind labels
first, judge revealed after, `min_human_agreement` default 0.9 over ≥30
labels). The operating protocol — cadence, what to do on disagreement — lives
in [RUNBOOK.md](../RUNBOOK.md).

## What a green calibration does NOT prove

- `MOCK_GRADED_JUDGE=band` calibrations prove wiring and band arithmetic,
  never judge quality — the mock scores each golden from its own label and is
  recorded as `judge_model: mock:band`, which can never unlock a paid run.
- A green calibration binds to what it measured: rubric sha, judge model and
  temperature, and the provider-resolved model id are all fingerprinted, and
  changing any of them stales it. It says nothing about a different rubric,
  a moved model snapshot, or dimensions you didn't write anchors for.

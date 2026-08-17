# support-reply — the worked example

A complete, real suite for a customer-support reply prompt, used as the
engine's onboarding path. Everything here is genuine content, not scaffold
placeholders: a 25-line production-style prompt, 12 realistic cases, four
deterministic contracts, a calibrated-judge rubric, a graded spec with a
declared `failTarget`, and 3 pass + 3 fail golden bundles.

```
bundle/       what `init --example support-reply` stages into input/ and adopts
candidate/    a rewrite with ONE deliberate change + the hypothesis behind it
WALKTHROUGH.md  the full guided tour: free stages verbatim, paid stages honestly
```

Start here:

```bash
uv run prompt-eval init --example support-reply
uv run prompt-eval validate
```

Then follow `WALKTHROUGH.md`. The free stages (adopt, validate, band-mock
calibrate) cost $0 and run in CI on every commit; the live stages spend real
API credit and are sized with `--dry-run` before you commit a dollar.

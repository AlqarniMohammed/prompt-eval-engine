# How this engine compares to the field

> **Staleness banner** — this survey was taken **August 2026** against the tool
> versions listed below. Evaluation tooling moves fast: treat every ❌ here as
> "verified absent *then*", re-check before repeating a claim, and read
> nothing after the survey date into this table. The engine itself does not
> depend on any claim below being true forever — the comparison exists to
> justify why this layer was built, not to disparage alternatives.

Surveyed (August 2026): **promptfoo 0.122.0** (source-verified — it is the engine
underneath), Inspect AI, DeepEval, Braintrust, Langfuse, LangSmith, OpenAI Evals,
Weave (W&B), Phoenix (Arize), Harbor.

## The capability table

| Capability | promptfoo alone | Hosted platforms (Braintrust, Langfuse, ...) | Inspect AI / DeepEval | **This engine** |
|---|---|---|---|---|
| Matrix runner, caching, resume, agent provider | ✅ (it *is* the engine here) | partial | partial | delegated to promptfoo |
| Run-level cost governance (measured pre-run gate, mid-run kill-switch, run lock) | ❌ | per-key/month caps only | per-sample caps only | ✅ |
| Judge calibration against golden fixtures, gating every graded run | ❌ | ❌ | ❌ | ✅ |
| Blinded, position-swapped pairwise promotion with verdict idempotency | unblinded `select-best` | ❌ | DeepEval ArenaGEval covers blinding | ✅ + idempotent verdicts |
| Pipeline state machine (content-hash staleness, recorded forces) | ❌ | ❌ | ❌ | ✅ |
| Dashboards | `promptfoo view` (per-case outputs) | ✅ hosted, team features | ❌ | local read-only dashboard (spend, pipeline, live runs, trends); per-case output browsing stays with `promptfoo view` |

## Per-claim sources

- **promptfoo rows** — verified by reading promptfoo 0.122.0 source (the pinned
  dependency in `package.json`), not its marketing pages. Specifically: no
  measured pre-run cost gate or run lock exists; `select-best` presents both
  outputs to one grader unblinded and without position swap; there is no
  golden-fixture judge-calibration concept; caching, resume, the eval matrix,
  and the claude-agent-sdk provider are its verified strengths — which is why
  every live run here IS a `promptfoo eval` (see `src/runner.py`).
- **Hosted platforms (Braintrust, Langfuse, LangSmith, Weave, Phoenix)** —
  product documentation as of the survey date. Spend controls found were
  API-key or monthly-budget scoped, not per-run measured gates with an
  in-flight kill switch; none documented judge calibration that *gates*
  scoring runs.
- **Inspect AI / DeepEval / OpenAI Evals / Harbor** — public docs and repos as
  of the survey date. Inspect AI caps per-sample cost; DeepEval's ArenaGEval
  covers pairwise blinding (credited in the table) but not verdict idempotency
  or promotion gating.

## What the table is NOT claiming

- Not that these tools are worse — they optimize for different jobs (hosted
  team UIs, model benchmarking, tracing). See "Use something else when" in the
  [README](../README.md).
- Not that the gaps are permanent. The claims carry their survey date on
  purpose.
- Not that this engine competes on breadth. It is deliberately narrow: prompt
  promotion decisions under hard spend ceilings, with a judge you can defend.

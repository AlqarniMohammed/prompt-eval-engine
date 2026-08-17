# Providers: who calls which model, and how to point them elsewhere

Three roles, three call paths. Everything below is declared in
`config/eval_config.yaml` — code never hardcodes a model.

## Role dispatch

| Role | Config | Call path | Default |
|---|---|---|---|
| Generation | `agents.generation` | promptfoo's native `anthropic:claude-agent-sdk` provider (per-test model/caps/workspace via test `options`) — or a per-suite HTTP provider, below | `claude-sonnet-5` |
| Judge | `agents.judge` | direct SDK call in `src/utils/model_client.py`, dispatched by model prefix: `openai:*` → OpenAI SDK, everything else → Anthropic SDK | `claude-sonnet-4-6` |
| Dataset | `agents.dataset` | same `model_client` dispatch, used by `gen-cases` / `perturb` drafts only | `claude-haiku-4-5` |

Every call path records into the same run ledger (`spend.jsonl`) with the
provider-reported usage and the **provider-resolved model id** — providers
update models behind stable names, and calibration binds to the resolved id
(`apiModel`), not the alias.

The judge must differ from the generation model — `src/config.py` raises on
equality; self-grading is hard-blocked, not warned about.

## Any-provider generation: the per-suite HTTP provider

promptfoo supports many providers, but the engine's per-test wiring
(workspaces, agent options) is built on the agent-sdk provider. The
sanctioned way to point *generation* at anything else — your deployed API, a
gateway, a local server — is a per-suite `provider:` block:

```yaml
suites:
  - id: my-endpoint-suite
    file: datasets/my-endpoint-suite.yaml
    rubric: judges/my-endpoint-suite.md
    provider:
      type: http
      url: https://api.example.com/generate
      # remaining keys pass to promptfoo's HTTP provider verbatim:
      # method, headers, body, transformResponse ...
```

The runner materializes a per-run promptfoo config for it (provider dicts
are honored at config level only), skips workspaces and agent-sdk options,
and — because the endpoint bills its owner, invisibly — ledgers generation
rows as `cost_unknown` at $0 with a loud alert: **the cost ceiling governs
judge spend only on http runs**. Header values never land in the manifest.
Full behavior list in the [README](../README.md); implementation in
`src/runner.py` (`_materialize_http_config`).

This is also the honest route to **real multi-turn** (an endpoint that
accepts a message array) and to **Ollama-style local generation**: run the
local server, point `provider.url` at it, and set a pricing row of zeros for
the model name it reports (local inference is free, but an unknown model
would otherwise price at the most-expensive row — see below).

## Judges beyond the default

- **Cross-family judge** (recommended against same-family bias): set
  `agents.judge.model: openai:<model>` and install the extra
  (`uv sync --extra cross-judge`). `doctor` and `preflight` verify the SDK
  and `OPENAI_API_KEY` before any paid path runs. **Caveat, stated where it
  matters**: the `openai:` judge path in this repo has never taken a live
  call — the wiring is tested against mocks; calibrate on a tiny suite first
  and treat the first live pass as the real proof
  ([RUNBOOK.md](../RUNBOOK.md) says the same, louder).
- **Hosted/self-hosted judge**: there is no `http` judge path. The judge is
  deliberately a direct, metered SDK call — it must record usage, the
  resolved model id, and temperature into the ledger and calibration record,
  which an arbitrary endpoint cannot promise. If your judge is only
  reachable over HTTP, front it with an OpenAI-compatible gateway and use the
  `openai:` dispatch with `OPENAI_BASE_URL`.
- A judge model change (or temperature change) stales every suite's
  calibration at once — the state machine enforces the recalibration.

## Pricing: extending the table

`pricing:` in `config/eval_config.yaml` is USD per MTok per model, plus the
`last_verified` stamp `doctor` checks for staleness (>180 days warns). Rules
the code enforces (`pricing()` in `src/config.py`):

- **Unknown models price at the MOST EXPENSIVE configured row.** A mispriced
  or unlisted model can only over-count, never under-count — over-counting
  trips ceilings early, which is the safe failure.
- Add a row for every model any role can resolve to, including local models
  (`{ input: 0, output: 0, ... }`) — otherwise the most-expensive rule
  charges your free Ollama tokens at flagship rates and aborts runs early.
- Measured history beats the table wherever it exists: after the first real
  run, estimates come from `outputs/history/` records, and the table only
  prices individual usage blocks.

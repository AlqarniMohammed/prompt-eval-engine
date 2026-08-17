"""Provider-dispatching client for the engine's single-shot calls (the judge,
the dataset generator). The model string picks the SDK:

    claude-* / anthropic:*   anthropic SDK (streamed — the SDK refuses
                             non-streaming calls whose max_tokens implies a
                             >10-minute worst case; incident #9)
    openai:*                 openai SDK, via the optional cross-judge extra
                             (install with: uv sync --extra cross-judge)

Retries are FREE-ONLY: overloaded/5xx responses bill nothing, so they back
off (with jitter) and retry; anything that consumed tokens is never blindly
re-billed (incident #4). Every call lands on the run ledger via
cost_tracker; unknown models fall back to the MOST EXPENSIVE pricing row,
so a cross-family model can only over-count.

gemini:* is deliberately absent — same dispatch pattern, add it when there
is a live calibration budget to prove it. Shipping untestable provider code
is a liability, not a feature.
"""

from __future__ import annotations

import random
import time

from src.utils import cost_tracker

_FREE_STATUSES = (429, 500, 502, 503, 529)  # billed nothing — safe to retry
_anthropic_client = None
_openai_client = None


def call(agent_cfg: dict, kind: str, prompt: str) -> str:
    """One live call on the given agents: block entry ({model,
    max_tokens_per_turn, ...}). Returns the reply text.

    `temperature` is forwarded to the API only when the agent config sets it —
    an absent key means the provider default (some OpenAI reasoning models
    reject an explicit temperature)."""
    model = agent_cfg["model"]
    max_tokens = int(agent_cfg.get("max_tokens_per_turn", 24000))
    temperature = agent_cfg.get("temperature")
    if model.startswith("openai:"):
        return _call_openai(model, max_tokens, kind, prompt, temperature)
    return _call_anthropic(model, max_tokens, kind, prompt, temperature)


def _record(kind: str, model: str, usage: dict, max_tokens: int,
            stop_reason: str | None = None, api_model: str | None = None):
    # apiModel is the PROVIDER-reported concrete id (e.g. a dated snapshot),
    # not the config alias — calibration binds to it (item 14: providers
    # update models behind stable names).
    cost_tracker.record(kind, model, usage, stopReason=stop_reason, apiModel=api_model)
    # Two truncation signals, belt and suspenders: the provider's own stop
    # reason (authoritative) and the token-cap heuristic (catches providers
    # that report usage but no stop reason).
    if stop_reason in ("max_tokens", "length"):
        cost_tracker.record("alert", model, usd=0.0,
                            note=f"{kind} reply stopped on {stop_reason} — truncated by the provider")
    elif usage["output_tokens"] >= max_tokens:
        cost_tracker.record("alert", model, usd=0.0,
                            note=f"{kind} reply hit the {max_tokens}-token cap — likely truncated")


def _call_anthropic(model: str, max_tokens: int, kind: str, prompt: str,
                    temperature: float | None = None) -> str:
    global _anthropic_client
    import anthropic

    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    api_model = model.removeprefix("anthropic:")
    extra = {} if temperature is None else {"temperature": float(temperature)}
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with _anthropic_client.messages.stream(
                model=api_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                **extra,
            ) as stream:
                resp = stream.get_final_message()
            usage = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            }
            _record(kind, model, usage, max_tokens,
                    stop_reason=getattr(resp, "stop_reason", None),
                    api_model=getattr(resp, "model", None))
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            free = (getattr(e, "status_code", None) in _FREE_STATUSES
                    or isinstance(e, anthropic.APIConnectionError))
            last_error = e
            if not free or attempt == 3:
                raise
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise last_error  # unreachable, keeps the type checker honest


def _call_openai(model: str, max_tokens: int, kind: str, prompt: str,
                 temperature: float | None = None) -> str:
    global _openai_client
    try:
        import openai
    except ImportError:
        raise RuntimeError(
            f'model "{model}" needs the openai SDK — install the extra: '
            "uv sync --extra cross-judge"
        )
    if _openai_client is None:
        _openai_client = openai.OpenAI()
    api_model = model.removeprefix("openai:")
    extra = {} if temperature is None else {"temperature": float(temperature)}
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            resp = _openai_client.chat.completions.create(
                model=api_model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                **extra,
            )
            u = resp.usage
            details = getattr(u, "prompt_tokens_details", None)
            usage = {
                "input_tokens": u.prompt_tokens,
                "output_tokens": u.completion_tokens,
                "cache_read_input_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
                "cache_creation_input_tokens": 0,
            }
            _record(kind, model, usage, max_tokens,
                    stop_reason=getattr(resp.choices[0], "finish_reason", None),
                    api_model=getattr(resp, "model", None))
            return resp.choices[0].message.content or ""
        except Exception as e:
            # Duck-typed across openai SDK versions: retry only what billed
            # nothing (rate limits, 5xx, connection drops).
            status = getattr(e, "status_code", None)
            free = status in _FREE_STATUSES or e.__class__.__name__ == "APIConnectionError"
            last_error = e
            if not free or attempt == 3:
                raise
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise last_error

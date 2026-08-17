"""model_client provider dispatch + gen-cases draft generation — all offline."""

import sys
import types

import pytest
import yaml

from src import config
from src.science import gen_cases
from src.utils import model_client


@pytest.fixture(autouse=True)
def reset_clients(monkeypatch):
    monkeypatch.setattr(model_client, "_anthropic_client", None)
    monkeypatch.setattr(model_client, "_openai_client", None)
    monkeypatch.setattr(gen_cases, "_mock_calls", 0)


# ---------------------------------------------------------------- dispatch —
def test_openai_prefix_without_sdk_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)  # import returns None → attribute error path
    monkeypatch.delitem(sys.modules, "openai")
    monkeypatch.setattr("builtins.__import__", _blocking_import("openai"))
    with pytest.raises(RuntimeError, match="uv sync --extra cross-judge"):
        model_client.call({"model": "openai:gpt-5"}, "judge", "hi")


def _blocking_import(blocked):
    real_import = __import__

    def fake(name, *a, **kw):
        if name == blocked:
            raise ImportError(f"No module named '{blocked}'")
        return real_import(name, *a, **kw)
    return fake


def test_openai_prefix_uses_openai_sdk(monkeypatch):
    recorded = {}

    class FakeUsage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 10, 5, None

    class FakeCompletions:
        def create(self, **kw):
            recorded.update(kw)
            msg = types.SimpleNamespace(content="openai says hi")
            return types.SimpleNamespace(usage=FakeUsage(),
                                         choices=[types.SimpleNamespace(message=msg)])

    fake_openai = types.SimpleNamespace(
        OpenAI=lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=FakeCompletions())))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    ledger = []
    monkeypatch.setattr(model_client.cost_tracker, "record",
                        lambda kind, model, usage=None, **kw: ledger.append((kind, model, usage)))

    out = model_client.call({"model": "openai:gpt-5", "max_tokens_per_turn": 2000}, "judge", "hi")
    assert out == "openai says hi"
    assert recorded["model"] == "gpt-5" and recorded["max_completion_tokens"] == 2000
    assert "temperature" not in recorded  # unset in config → provider default
    kind, model, usage = ledger[0]
    # ledger keeps the prefixed id so pricing falls back to the most expensive row
    assert (kind, model) == ("judge", "openai:gpt-5")
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5


def test_anthropic_default_path_records_ledger(monkeypatch):
    class FakeStream:
        def __enter__(self):
            usage = types.SimpleNamespace(input_tokens=7, output_tokens=3,
                                          cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0)
            block = types.SimpleNamespace(type="text", text="claude says hi")
            msg = types.SimpleNamespace(usage=usage, content=[block])
            return types.SimpleNamespace(get_final_message=lambda: msg)

        def __exit__(self, *a):
            return False

    seen = {}

    class FakeMessages:
        def stream(self, **kw):
            seen.update(kw)
            return FakeStream()

    monkeypatch.setattr(model_client, "_anthropic_client",
                        types.SimpleNamespace(messages=FakeMessages()))
    ledger = []
    monkeypatch.setattr(model_client.cost_tracker, "record",
                        lambda kind, model, usage=None, **kw: ledger.append((kind, model)))

    out = model_client.call({"model": "claude-sonnet-4-6", "max_tokens_per_turn": 100}, "judge", "hi")
    assert out == "claude says hi"
    assert seen["model"] == "claude-sonnet-4-6"
    assert "temperature" not in seen  # unset in config → provider default
    assert ledger[0] == ("judge", "claude-sonnet-4-6")


def test_temperature_forwarded_when_configured(monkeypatch):
    """A configured temperature reaches both SDKs; 0 must not be dropped as falsy."""
    seen_anthropic, seen_openai = {}, {}

    class FakeStream:
        def __enter__(self):
            usage = types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                          cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0)
            block = types.SimpleNamespace(type="text", text="ok")
            msg = types.SimpleNamespace(usage=usage, content=[block])
            return types.SimpleNamespace(get_final_message=lambda: msg)

        def __exit__(self, *a):
            return False

    class FakeMessages:
        def stream(self, **kw):
            seen_anthropic.update(kw)
            return FakeStream()

    monkeypatch.setattr(model_client, "_anthropic_client",
                        types.SimpleNamespace(messages=FakeMessages()))

    class FakeUsage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 1, 1, None

    class FakeCompletions:
        def create(self, **kw):
            seen_openai.update(kw)
            msg = types.SimpleNamespace(content="ok")
            return types.SimpleNamespace(usage=FakeUsage(),
                                         choices=[types.SimpleNamespace(message=msg)])

    fake_openai = types.SimpleNamespace(
        OpenAI=lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=FakeCompletions())))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setattr(model_client.cost_tracker, "record", lambda *a, **kw: None)

    model_client.call({"model": "claude-sonnet-4-6", "temperature": 0}, "judge", "hi")
    assert seen_anthropic["temperature"] == 0.0
    model_client.call({"model": "openai:gpt-5", "temperature": 0}, "judge", "hi")
    assert seen_openai["temperature"] == 0.0


def test_cross_family_judge_passes_config_hard_block(monkeypatch, fresh_config):
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai:gpt-5")
    agents = fresh_config.agents()
    assert agents["judge"]["model"] == "openai:gpt-5"
    assert agents["generation"]["model"] != agents["judge"]["model"]


def test_unknown_model_pricing_falls_back_to_most_expensive():
    row = config.pricing("openai:gpt-5")
    model_rows = [r for r in config.get()["pricing"].values() if isinstance(r, dict)]
    assert row == max(model_rows, key=lambda r: r.get("output", 0))


# --------------------------------------------------------------- gen-cases —
def test_gen_cases_canned_writes_draft_yaml(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "canned")
    result = gen_cases.generate_cases("demo-suite", 3, out_dir=tmp_path)
    text = result["out"].read_text()
    assert text.startswith("# DRAFT")
    doc = yaml.safe_load(text)
    assert doc["suite"] == "demo-suite"
    assert len(doc["candidates"]) == 3
    assert all(c["id"] and c["probe"] for c in doc["candidates"])


def test_gen_cases_malformed_once_reasks_then_succeeds(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "malformed-once")
    result = gen_cases.generate_cases("demo-suite", 2, out_dir=tmp_path)
    assert len(result["cases"]) == 2


def test_gen_cases_malformed_raises_after_one_reask(synthetic_project, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DATASET", "malformed")
    with pytest.raises(RuntimeError, match="after one re-ask"):
        gen_cases.generate_cases("demo-suite", 2, out_dir=tmp_path)


def test_gen_cases_prompt_carries_realism_charter(synthetic_project):
    prompt = gen_cases._build_prompt("demo-suite", 4)
    assert "ORDINARY, busy user" in prompt
    assert "do NOT write best-practice prompts" in prompt
    # topic context from the suite spec is present
    assert "system prompt under test" in prompt


def test_gen_cases_live_path_uses_dataset_agent(synthetic_project, tmp_path, monkeypatch):
    calls = {}

    def fake_call(agent_cfg, kind, prompt):
        calls.update(agent=agent_cfg, kind=kind)
        return '{"cases": [{"id": "a", "probe": "hey can u check this"}]}'

    monkeypatch.setattr(gen_cases.model_client, "call", fake_call)
    result = gen_cases.generate_cases("demo-suite", 1, out_dir=tmp_path)
    assert calls["kind"] == "dataset"
    assert calls["agent"]["model"] == config.get()["agents"]["dataset"]["model"]
    assert result["model"] == calls["agent"]["model"]


def test_anthropic_stop_reason_recorded_and_truncation_alerted(monkeypatch):
    """The provider's own stop_reason lands on the ledger; max_tokens raises
    a truncation alert grounded in that signal (not the cap heuristic)."""
    import types as _t

    class FakeStream:
        def __enter__(self):
            usage = _t.SimpleNamespace(input_tokens=7, output_tokens=3,
                                       cache_read_input_tokens=0,
                                       cache_creation_input_tokens=0)
            block = _t.SimpleNamespace(type="text", text="cut off mid-")
            msg = _t.SimpleNamespace(usage=usage, content=[block],
                                     stop_reason="max_tokens")
            return _t.SimpleNamespace(get_final_message=lambda: msg)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(model_client, "_anthropic_client",
                        _t.SimpleNamespace(messages=_t.SimpleNamespace(stream=lambda **kw: FakeStream())))
    ledger = []
    monkeypatch.setattr(model_client.cost_tracker, "record",
                        lambda kind, model, usage=None, usd=None, **kw: ledger.append((kind, kw)))

    model_client.call({"model": "claude-sonnet-4-6", "max_tokens_per_turn": 100}, "judge", "hi")
    kinds = [k for k, _ in ledger]
    assert kinds == ["judge", "alert"]
    assert ledger[0][1].get("stopReason") == "max_tokens"
    assert "stopped on max_tokens" in ledger[1][1].get("note", "")

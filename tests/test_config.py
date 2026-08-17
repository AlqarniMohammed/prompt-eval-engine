import pytest

from src import config


def test_template_config_loads_and_validates():
    cfg = config.load(force_reload=True)
    assert cfg["version"] == 2
    assert config.suites() == []  # the repo ships bare — no example suites


def test_shipped_config_hashes_durable_raw_text():
    # Public repos track outputs/history/; the shipped default must never
    # store verbatim prompt text in durable records.
    cfg = config.load(force_reload=True)
    assert cfg["governance"]["redaction"]["history_raw_text"] == "hash"


def test_redaction_mode_typo_is_a_hard_error():
    # durable_text() falls through to verbatim storage on any unknown mode —
    # a typo must die in validation, not silently fail open.
    import copy
    cfg = copy.deepcopy(config.load(force_reload=True))
    cfg["governance"]["redaction"]["history_raw_text"] = "Hash"
    with pytest.raises(config.ConfigError, match="history_raw_text"):
        config._validate(cfg)


def test_synthetic_project_declares_suites(synthetic_project):
    assert len(config.suites()) == 2
    suite = config.suite_by_id(synthetic_project.suite_id)
    assert suite and suite["rubric"].endswith("demo.md")
    assert config.asserts_file().name == "asserts.py"


def test_judge_temperature_copied_through_agents(synthetic_project):
    assert config.agents()["judge"]["temperature"] == 0


def test_judge_equals_generator_is_hard_blocked(monkeypatch):
    monkeypatch.setenv("EVAL_JUDGE_MODEL", config.get()["agents"]["generation"]["model"])
    with pytest.raises(config.ConfigError, match="hard-blocked"):
        config.agents()


def test_env_overrides_applied_and_reported(monkeypatch):
    monkeypatch.setenv("EVAL_MODEL", "claude-opus-5")
    monkeypatch.setenv("EVAL_REPEAT", "2")
    agents = config.agents()
    assert agents["generation"]["model"] == "claude-opus-5"
    assert agents["generation"]["repeat"] == 2
    used = config.overrides_used()
    assert used["EVAL_MODEL"] == "claude-opus-5"
    assert used["EVAL_REPEAT"] == "2"


def test_only_honored_overrides_are_recorded(monkeypatch):
    # EVAL_FORCE / EVAL_MAX_COST have no readers — recording them as honored
    # overrides would be manifest dishonesty (audit finding 12).
    monkeypatch.setenv("EVAL_FORCE", "1")
    monkeypatch.setenv("EVAL_MAX_COST", "9")
    used = config.overrides_used()
    assert "EVAL_FORCE" not in used
    assert "EVAL_MAX_COST" not in used


def test_unknown_model_prices_at_most_expensive_row():
    fable = config.pricing("claude-fable-5")
    assert config.pricing("claude-nonexistent-9") == fable
    assert fable["output"] >= config.pricing("claude-sonnet-5")["output"]

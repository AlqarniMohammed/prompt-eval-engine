"""First-contact gates found in the pre-launch dogfood audit: doctor must not
report green on the .env.example placeholder key, must check the running Node
against package.json engines, and a *paid* calibrate must refuse placeholder
goldens (the band mock stays allowed — it is the free wiring check)."""

import pytest

from src import runner, state


def test_doctor_rejects_the_env_example_placeholder_key(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "YOUR_API_KEY")
    rc = runner.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "still the .env.example placeholder" in out
    assert "doctor: FAILED" in out


def test_doctor_warns_on_unrecognized_key_shape(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-an-anthropic-key")
    runner.main(["doctor"])
    out = capsys.readouterr().out
    assert "does not look like an Anthropic key" in out


def test_doctor_checks_node_version(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    runner.main(["doctor"])
    out = capsys.readouterr().out
    assert "node v" in out  # either "ok node vX (>= floor)" or the too-old ERROR


SUITE = "demo-suite"


def test_paid_calibrate_refuses_placeholder_goldens(synthetic_project, capsys):
    (synthetic_project.root / "fixtures/golden/demo/pass.txt").write_text(
        "===== STDOUT =====\nEVAL-INIT-PLACEHOLDER hello\n")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    rc = runner.main(["calibrate", "--suite", SUITE])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ABORTED BEFORE ANY CALL" in err
    assert "EVAL-INIT-PLACEHOLDER" in err
    assert "calibrated" not in state.suite_state(SUITE)


def test_band_mock_calibrate_still_allowed_on_placeholders(
        synthetic_project, monkeypatch, capsys):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "band")
    (synthetic_project.root / "fixtures/golden/demo/pass.txt").write_text(
        "===== STDOUT =====\nEVAL-INIT-PLACEHOLDER hello\n")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    rc = runner.main(["calibrate", "--suite", SUITE])
    assert rc == 0
    assert "CALIBRATION GREEN" in capsys.readouterr().out

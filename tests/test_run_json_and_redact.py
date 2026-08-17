"""E9: consolidated run.json, report --format json, secret redaction."""

import json

from src import config, runner
from src.reports import summary
from src.utils import redact


def _fabricate_run(tmp_path):
    run_dir = tmp_path / "graded-20260815-000000"
    run_dir.mkdir()
    rows = [{
        "testCase": {"description": "c1 polite", "vars": {
            "suite": "demo-suite", "cell": "c1", "trial": 1, "promptVariant": None}},
        "success": False,
        "gradingResult": {"componentResults": [
            {"namedScores": {"warmth": 2}, "pass": False, "reason": "cold reply"}]},
    }, {
        "testCase": {"description": "c2 rushed", "vars": {
            "suite": "demo-suite", "cell": "c2", "trial": 1}},
        "success": True,
        "gradingResult": {"componentResults": [{"namedScores": {"warmth": 5}, "pass": True}]},
    }]
    (run_dir / "results.json").write_text(json.dumps({"results": {"results": rows}}))
    (run_dir / "spend.jsonl").write_text("\n".join([
        json.dumps({"kind": "generation", "usd": 0.5, "at": "2026-08-15T00:00:00+00:00"}),
        json.dumps({"kind": "judge", "usd": 0.1, "at": "2026-08-15T00:00:01+00:00"}),
        json.dumps({"kind": "cache_hit", "usd": 0.0, "saved_usd": 0.25, "at": "2026-08-15T00:00:02+00:00"}),
        json.dumps({"kind": "alert", "usd": 0.0, "note": "something odd", "at": "2026-08-15T00:00:03+00:00"}),
    ]) + "\n")
    (run_dir / "status.jsonl").write_text(json.dumps(
        {"ev": "case_end", "case": "c1", "truncated": True}) + "\n")
    manifest = {"runId": run_dir.name, "mode": "graded", "suite": "demo-suite",
                "verbatimConfig": "secret stuff"}
    return run_dir, manifest


def test_write_run_json_consolidates_everything(synthetic_project, tmp_path):
    run_dir, manifest = _fabricate_run(tmp_path)
    out = summary.write_run_json(run_dir, manifest)
    doc = json.loads(out.read_text())
    assert doc["schemaVersion"] == 1
    assert "verbatimConfig" not in doc["manifest"]
    assert doc["spend"]["totalUsd"] == 0.6
    assert doc["cache"] == {"hits": 1, "savedUsd": 0.25}
    assert doc["alerts"] == ["something odd"]
    assert doc["truncatedCells"] == ["c1"]
    r1 = next(r for r in doc["rows"] if r["cell"] == "c1")
    assert r1["success"] is False and r1["truncated"] is True
    assert r1["failedReasons"] == ["cold reply"]
    r2 = next(r for r in doc["rows"] if r["cell"] == "c2")
    assert r2["success"] is True and r2["truncated"] is False


def test_report_format_json(synthetic_project, tmp_path, capsys):
    run_dir, manifest = _fabricate_run(tmp_path)
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    rc = runner.main(["report", str(run_dir), "--format", "json"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schemaVersion"] == 1 and doc["manifest"]["runId"] == run_dir.name


def test_scrub_known_secret_shapes(synthetic_project):
    text = (
        "key sk-ant-api03-AAAABBBBcccc111 and sk-proj-aaaaaaaaaaaaaaaaaaaaaaa "
        "aws AKIAIOSFODNN7EXAMPLE gh ghp_abcdefghijklmnopqrstuv "
        "Authorization: Bearer abcdef0123456789TOKEN "
        "-----BEGIN RSA PRIVATE KEY-----")
    scrubbed, hits = redact.scrub(text)
    assert hits >= 5
    for secret in ("sk-ant-api03", "AKIAIOSFODNN7EXAMPLE", "ghp_abcdefghijklmnopqrstuv",
                   "Bearer abcdef", "BEGIN RSA PRIVATE KEY"):
        assert secret not in scrubbed
    assert "«REDACTED:anthropic-key»" in scrubbed


def test_durable_text_policy_modes(synthetic_project, monkeypatch):
    assert redact.durable_text(None) is None
    assert redact.durable_text("plain text") == "plain text"

    def with_mode(mode):
        cfg = {k: v for k, v in config.get().items()}
        cfg["governance"] = {**cfg["governance"], "redaction": {"history_raw_text": mode}}
        monkeypatch.setattr(config, "_cached", cfg)

    with_mode("hash")
    hashed = redact.durable_text("some prompt text")
    assert set(hashed) == {"sha256", "chars"} and hashed["chars"] == len("some prompt text")
    with_mode("omit")
    assert redact.durable_text("some prompt text") is None
    with_mode("keep")
    assert "«REDACTED:anthropic-key»" in redact.durable_text("k = sk-ant-api03-XXXXYYYY")


def test_custom_patterns_from_config(synthetic_project, monkeypatch):
    cfg = {k: v for k, v in config.get().items()}
    cfg["governance"] = {**cfg["governance"],
                         "redaction": {"patterns": [r"ACME-[0-9]{6}"]}}
    monkeypatch.setattr(config, "_cached", cfg)
    scrubbed, hits = redact.scrub("ticket ACME-123456 leaked")
    assert hits == 1 and "ACME-123456" not in scrubbed

"""E7: hash-chained history ledgers + `history verify`."""

import json

from src import config, runner
from src.utils import chain


def test_append_and_verify_clean(tmp_path):
    path = tmp_path / "ledger.jsonl"
    for i in range(3):
        chain.append_chained(path, {"n": i})
    assert chain.verify_chain(path) == []
    entries = [json.loads(l) for l in path.read_text().splitlines()]
    assert entries[0]["prev"] == "genesis"
    assert entries[1]["prev"] == entries[0]["hash"]
    assert entries[2]["prev"] == entries[1]["hash"]


def test_modified_middle_line_breaks_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    for i in range(3):
        chain.append_chained(path, {"n": i})
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["n"] = 99
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n")
    problems = chain.verify_chain(path)
    assert any("hash mismatch" in p for p in problems)


def test_deleted_line_breaks_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    for i in range(3):
        chain.append_chained(path, {"n": i})
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert any("prev-hash mismatch" in p for p in chain.verify_chain(path))


def test_legacy_prefix_is_anchored(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"legacy": 1}\n{"legacy": 2}\n')
    chain.append_chained(path, {"n": 0})
    assert chain.verify_chain(path) == []
    # editing the legacy prefix breaks the anchor
    path.write_text(path.read_text().replace('{"legacy": 1}', '{"legacy": 9}'))
    assert any("prev-hash mismatch" in p for p in chain.verify_chain(path))


def test_unchained_line_after_chain_start_is_a_problem(tmp_path):
    path = tmp_path / "ledger.jsonl"
    chain.append_chained(path, {"n": 0})
    with path.open("a") as f:
        f.write('{"sneaky": true}\n')
    assert any("unchained entry" in p for p in chain.verify_chain(path))


def test_history_verify_command(synthetic_project, capsys, tmp_path):
    hist = config.history_dir()
    hist.mkdir(parents=True, exist_ok=True)
    record = hist / "compare-demo-suite-1.json"
    record.write_text('{"promote": true}')
    from src import state as state_lib
    chain.append_chained(hist / "verdicts.jsonl",
                         {"key": "k", "record": record.name,
                          "recordSha": state_lib.sha256_file(record), "promote": True})
    assert runner.main(["history", "verify"]) == 0
    assert "chain intact" in capsys.readouterr().out
    # now tamper with the fat record
    record.write_text('{"promote": false}')
    assert runner.main(["history", "verify"]) == 1
    captured = capsys.readouterr()
    assert "does not match its recorded sha" in captured.err


def test_history_verify_with_nothing(synthetic_project, capsys):
    assert runner.main(["history", "verify"]) == 0
    assert "nothing to verify" in capsys.readouterr().out

"""P2 prerequisites — pricing freshness metadata (and the non-dict-row crash
it required fixing), the MOCK_GRADED_JUDGE=band offline-calibration mock
(proves wiring and band arithmetic, never judge quality), and the reviewed
rubric template library behind `init --rubric`."""

import pytest
import yaml

from src import config, runner, state
from src.scaffold.templates import PLACEHOLDER, RUBRIC_LIBRARY, rubric_md
from src.utils import rubric as rubric_lib

SUITE = "demo-suite"


# ------------------------------------------------------ pricing metadata —
def _patch_pricing(project, pricing):
    path = project.root / "eval_config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["pricing"] = pricing
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    config.load(force_reload=True)


def test_pricing_tolerates_metadata_rows(synthetic_project):
    _patch_pricing(synthetic_project, {
        "last_verified": "2026-08-16",
        "cheap": {"input": 1, "output": 5},
        "dear": {"input": 10, "output": 50},
    })
    assert config.pricing("cheap")["output"] == 5
    # unknown model → most expensive DICT row; the string row must not crash max()
    assert config.pricing("nope")["output"] == 50


def test_pricing_with_no_model_rows_raises(synthetic_project):
    _patch_pricing(synthetic_project, {"last_verified": "2026-08-16"})
    with pytest.raises(config.ConfigError, match="no model rows"):
        config.pricing("anything")


@pytest.mark.parametrize("stamp,expect", [
    (None, "not set"),
    ("yesterday", "not a YYYY-MM-DD"),
    ("2020-01-01", "days ago"),
    ("2026-08-01", None),
])
def test_pricing_staleness_warning(synthetic_project, stamp, expect):
    pricing = {"cheap": {"input": 1, "output": 5}}
    if stamp:
        pricing["last_verified"] = stamp
    _patch_pricing(synthetic_project, pricing)
    warning = runner._pricing_staleness_warning()
    if expect is None:
        assert warning is None
    else:
        assert warning and expect in warning


def test_shipped_config_has_a_fresh_stamp():
    cfg = yaml.safe_load((config.ROOT / "config" / "eval_config.yaml").read_text())
    stamp = cfg["pricing"]["last_verified"]
    assert isinstance(stamp, str) and len(stamp) == 10


# ------------------------------------------------------------- band mock —
def test_band_mock_calibration_goes_green(synthetic_project, monkeypatch, capsys):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "band")
    # a mid golden exercises the third band; mean must land in (2.5, 3.5)
    (synthetic_project.root / "fixtures/golden/demo/mid.txt").write_text(
        "===== STDOUT =====\nHello. The answer is 42.\n")
    state.record(SUITE, "validated", state.validate_fingerprint(SUITE))
    rc = runner.main(["calibrate", "--suite", SUITE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CALIBRATION GREEN" in out
    cal = state.suite_state(SUITE)["calibrated"]
    # the mock must never masquerade as the configured judge model
    assert cal["judge_model"] == "mock:band"
    assert cal["green"] is True


def test_band_mock_scores_follow_the_golden_label(synthetic_project, monkeypatch):
    from src.evaluators.llm_judge import judge_bundle
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "band")
    for label, expected in (("demo/pass.txt#0", 5), ("demo/fail.txt#1", 2),
                            ("demo/mid.txt#2", 3)):
        scores = judge_bundle(SUITE, "===== STDOUT =====\nx", {"golden": label})["scores"]
        assert set(scores.values()) == {expected}, label
    # non-golden calls (graded cells) score a flat passing 4
    scores = judge_bundle(SUITE, "===== STDOUT =====\nx", {"cell": "tone-polite"})["scores"]
    assert set(scores.values()) == {4}


# ------------------------------------------------------- rubric library —
def test_every_library_rubric_parses_with_full_anchors(tmp_path):
    assert set(RUBRIC_LIBRARY) == {"summarization", "extraction", "classification",
                                   "rag", "support-chat", "codegen", "rewrite"}
    for kind in RUBRIC_LIBRARY:
        text = rubric_md("some-suite", kind)
        path = tmp_path / f"{kind}.md"
        path.write_text(text)
        parsed = rubric_lib.parse(path)  # raises on missing anchors / bad names
        assert 3 <= len(parsed["dimensions"]) <= 5, kind
        # exactly ONE placeholder review line — the anchors themselves are done
        assert text.count(PLACEHOLDER) == 1, kind
        for dim in parsed["dimensions"]:
            assert "TODO" not in "".join(dim["anchors"].values()), (kind, dim["name"])


def test_generic_rubric_unchanged_without_kind():
    text = rubric_md("some-suite")
    assert text.count(PLACEHOLDER) > 1          # the fill-in scaffold, untouched
    assert "## dimension: correctness" in text


def test_cli_accepts_rubric_kinds():
    args = runner.build_parser().parse_args(["init", "--rubric", "support-chat"])
    assert args.rubric == "support-chat"
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(["init", "--rubric", "poetry"])


def test_init_scaffolds_library_rubric(synthetic_project, monkeypatch):
    from src.scaffold import detect, register
    dropped = synthetic_project.root / "input"
    dropped.mkdir(exist_ok=True)
    prompt = dropped / "triage-notes.md"
    prompt.write_text("Summarize the incident notes for the on-call engineer.")
    det = detect.detect(prompt, as_kind="prompt")
    register._scaffold(det, dry=False, rubric_kind="summarization")
    rubric_path = synthetic_project.root / "judges" / "triage-notes.md"
    text = rubric_path.read_text()
    assert "## dimension: faithfulness" in text
    assert text.count(PLACEHOLDER) == 1


def test_bundle_refuses_rubric_kind(synthetic_project, tmp_path):
    from src.scaffold import register
    from types import SimpleNamespace
    det = SimpleNamespace(kind="bundle", suite_id="b1")
    with pytest.raises(register.InitError, match="brings its own rubric"):
        register.process(det, rubric_kind="rag")

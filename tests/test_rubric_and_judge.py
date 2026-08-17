import pytest

from src import config
from src.evaluators import llm_judge
from src.evaluators.graded_judge import graded_judge_assert
from src.utils import rubric as rubric_lib


def test_all_declared_rubrics_parse_with_complete_anchors(synthetic_project):
    for suite in config.suites():
        if not suite.get("rubric"):
            continue
        r = rubric_lib.parse(config.resolve(suite["rubric"]))
        assert 3 <= len(r["dimensions"]) <= 6, f'{suite["id"]}: {len(r["dimensions"])} dimensions'
        for d in r["dimensions"]:
            assert set(d["anchors"]) == set("12345")


def test_rubric_missing_anchor_is_an_error(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("# Rubric: x\n## dimension: warmth\nWhat it measures: warmth\n"
                   "- 5: great\n- 4: good\n- 3: ok\n- 2: poor\n")  # no anchor for 1
    with pytest.raises(ValueError, match="missing anchor for score 1"):
        rubric_lib.parse(bad)


def test_judge_prompt_skeleton(synthetic_project):
    r = rubric_lib.for_suite("demo-suite")
    prompt = rubric_lib.build_judge_prompt(r, "demo-suite", "===== STDOUT =====\nx")
    assert "QUOTE the exact lines" in prompt
    assert "Return ONLY this JSON" in prompt
    assert all(d["name"] in prompt for d in r["dimensions"])
    # judge is never asked for an overall score
    assert "overall" not in prompt.lower().replace("overall.", "")
    # verbosity-bias guard (MT-Bench Table 3): length must not earn points
    assert "Do not reward length" in prompt


def test_extract_json_object_survives_chatty_judge():
    text = 'Sure! {"a": {"b": "c}"}, "n": 1} hope that helps'
    assert llm_judge.extract_json_object(text) == '{"a": {"b": "c}"}, "n": 1}'


@pytest.mark.parametrize("mode,expect_pass", [("good", True), ("bad", False)])
def test_mock_judge_modes(synthetic_project, monkeypatch, mode, expect_pass):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", mode)
    result = graded_judge_assert("===== STDOUT =====\nx",
                                 {"vars": {"suite": "demo-suite", "cell": f"c-{mode}"}})
    assert result["pass"] is expect_pass
    assert set(result["namedScores"]) == {d["name"] for d in rubric_lib.for_suite("demo-suite")["dimensions"]}


def test_malformed_judge_is_error_not_fabricated_score(synthetic_project, monkeypatch):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "malformed")
    result = graded_judge_assert("===== STDOUT =====\nx",
                                 {"vars": {"suite": "demo-suite", "cell": "c-mal"}})
    assert result["pass"] is False
    assert "JUDGE-ERROR" in result["reason"]
    assert "namedScores" not in result


def test_malformed_once_recovers_via_single_reask(synthetic_project, monkeypatch):
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "malformed-once")
    result = graded_judge_assert("===== STDOUT =====\nx",
                                 {"vars": {"suite": "demo-suite", "cell": "c-once"}})
    assert result["pass"] is True  # scores of 4 with threshold 3


def test_mock_judge_never_masquerades_as_real_model(synthetic_project, monkeypatch):
    """The calibration state gate matches on judge_model — a mock calibration
    reporting the real judge model would open the graded gate for free."""
    monkeypatch.setenv("MOCK_GRADED_JUDGE", "good")
    out = llm_judge.judge_bundle("demo-suite", "bundle text")
    assert out["judge_model"] == "mock:good"

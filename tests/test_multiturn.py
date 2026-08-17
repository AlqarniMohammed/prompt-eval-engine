"""E17 — multi-turn simulation: `messages:` on cases becomes a labeled
simulated-transcript block in the prompt, validated free (alternating roles,
last turn user, exclusive with probe), flagged in the manifest. Honest scope:
this is a single-prompt simulation, never real message-array turns."""

import pytest
import yaml

from src import config, runner
from src.loaders.pf_prompts import current_prompt
from src.reports import checks
from src.utils import dataset_loader

SUITE = "demo-suite"

GOOD = [
    {"role": "user", "content": "my order arrived broken"},
    {"role": "assistant", "content": "So sorry — I've issued a refund."},
    {"role": "user", "content": "it's been a week and no refund"},
]


# ------------------------------------------------------------- validation —
def test_valid_messages_have_no_problems():
    assert dataset_loader.message_problems(GOOD) == []


@pytest.mark.parametrize("messages,expect", [
    ("not a list", "non-empty YAML list"),
    ([], "non-empty YAML list"),
    ([{"role": "system", "content": "x"}], "role must be user or assistant"),
    ([{"role": "user", "content": "  "}], "non-empty string"),
    ([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], "must alternate"),
    ([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
     "last turn must be user"),
])
def test_bad_messages_are_reported(messages, expect):
    problems = dataset_loader.message_problems(messages)
    assert problems and any(expect in p for p in problems)


def test_assistant_first_is_allowed():
    # A support conversation legitimately opens with the assistant's greeting.
    msgs = [{"role": "assistant", "content": "Hi, how can I help?"},
            {"role": "user", "content": "where is my order?"}]
    assert dataset_loader.message_problems(msgs) == []


# ------------------------------------------------------------- rendering —
def test_render_labels_prior_turns_and_ends_with_live_request():
    text = dataset_loader.render_messages(GOOD)
    assert "SIMULATED CONVERSATION" in text and "not real multi-turn" in text
    assert "user: my order arrived broken" in text
    assert "assistant: So sorry" in text
    assert text.endswith("The user's next message:\nit's been a week and no refund")


# ------------------------------------------------------- loader adoption —
def _write_dataset(project, case):
    path = project.root / "datasets/demo-suite.yaml"
    data = yaml.safe_load(path.read_text())
    data.append(case)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_load_cases_converts_messages_to_transcript_var(synthetic_project):
    _write_dataset(synthetic_project, {
        "description": "refund follow-up",
        "messages": GOOD,
        "vars": {"promptFile": "demo.md", "golden": "demo"},
    })
    case = next(c for c in dataset_loader.load_cases(SUITE)
                if c["description"] == "refund follow-up")
    assert "messages" not in case
    assert "no refund" in case["vars"]["simulatedTranscript"]


def test_load_cases_refuses_messages_plus_probe(synthetic_project):
    _write_dataset(synthetic_project, {
        "description": "conflicted",
        "messages": GOOD,
        "vars": {"promptFile": "demo.md", "golden": "demo", "probe": "also this"},
    })
    with pytest.raises(ValueError, match="mutually.*exclusive"):
        dataset_loader.load_cases(SUITE)


def test_load_cases_refuses_invalid_messages(synthetic_project):
    _write_dataset(synthetic_project, {
        "description": "trailing assistant",
        "messages": GOOD[:2],
        "vars": {"promptFile": "demo.md", "golden": "demo"},
    })
    with pytest.raises(ValueError, match="last turn must be user"):
        dataset_loader.load_cases(SUITE)


def test_graded_matrix_messages_are_adopted(synthetic_project):
    (synthetic_project.root / "datasets/graded" / f"{SUITE}.yaml").write_text(yaml.safe_dump([
        {"description": "cell with history", "messages": GOOD,
         "vars": {"promptFile": "demo.md", "golden": "demo", "cell": "mt1"}},
    ], sort_keys=False))
    case = dataset_loader.load_graded_cases(SUITE)[0]
    assert "messages" not in case
    assert "simulatedTranscript" in case["vars"]


# ------------------------------------------------------ prompt building —
def test_prompt_appends_transcript_block(synthetic_project):
    built = current_prompt({"vars": {
        "promptFile": "demo.md",
        "simulatedTranscript": dataset_loader.render_messages(GOOD)}})
    assert built.startswith("Greet the user warmly")
    assert "SIMULATED CONVERSATION" in built
    assert built.rstrip().endswith("it's been a week and no refund")


def test_prompt_refuses_probe_plus_transcript(synthetic_project):
    with pytest.raises(ValueError, match="probe"):
        current_prompt({"vars": {"promptFile": "demo.md", "probe": "x",
                                 "simulatedTranscript": "y"}})


# ------------------------------------------------------- offline checks —
def test_checks_flag_bad_messages_and_probe_conflict(synthetic_project):
    _write_dataset(synthetic_project, {
        "description": "bad multiturn",
        "messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        "vars": {"promptFile": "demo.md", "golden": "demo", "probe": "x"},
    })
    errors, _ = checks.run_checks()
    assert any("must alternate" in e for e in errors)
    assert any("mutually exclusive" in e for e in errors)


def test_checks_flag_graded_matrix_messages(synthetic_project):
    (synthetic_project.root / "datasets/graded" / f"{SUITE}.yaml").write_text(yaml.safe_dump([
        {"vars": {"promptFile": "demo.md", "cell": "mt1"},
         "messages": [{"role": "assistant", "content": "hi"}]},
    ], sort_keys=False))
    errors, _ = checks.run_checks()
    assert any("graded/" in e and "last turn must be user" in e for e in errors)


# ------------------------------------------------------------- manifest —
def test_manifest_flags_simulated_multiturn(synthetic_project):
    plain = [{"vars": {"promptFile": "demo.md"}}]
    simulated = [{"vars": {"promptFile": "demo.md", "simulatedTranscript": "x"}}]
    assert runner._build_manifest("graded", None, plain, 1, 1, [])["simulatedMultiTurn"] is False
    assert runner._build_manifest("graded", None, simulated, 1, 1, [])["simulatedMultiTurn"] is True

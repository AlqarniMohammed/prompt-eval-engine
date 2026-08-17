"""`prompt-eval init` — detection branches, per-type end-to-end scaffolds,
config-edit safety (comment preservation, rollback), idempotency/collision,
the preflight default-suite fix, and the round scaffold line. All offline."""

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src import config
from src.reports import checks
from src.scaffold import config_edit, detect, register, templates


def _ns(**kw):
    base = dict(path=None, as_kind=None, suite=None, yes=True,
                dry_run=False, print_config=False, rubric=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    """A bare third-party project: zero suites, commented asserts_file,
    comments in the config that edits must preserve."""
    root = tmp_path / "proj"
    for d in ("prompts/production", "prompts/candidates", "datasets/graded-specs",
              "datasets/graded", "judges", "fixtures/golden", "outputs", "input"):
        (root / d).mkdir(parents=True)
    cfg = f"""version: 2

project:
  name: fresh
  production_model: claude-sonnet-5
  # asserts_file: project/asserts.py

paths:
  fixtures_dir: {root}/fixtures
  graded_dir: {root}/datasets/graded
  outputs_dir: {root}/outputs

prompts:
  production_dir: {root}/prompts/production
  candidates_dir: {root}/prompts/candidates

suites:
  # a load-bearing comment the edit must not eat
  []

agents:
  generation:
    model: claude-sonnet-5
    max_budget_usd: 3
    max_tokens_per_turn: 64000
  judge:
    model: claude-sonnet-4-6
    max_tokens_per_turn: 24000
  dataset:
    model: claude-haiku-4-5

pricing:
  claude-sonnet-5: {{ input: 3, output: 15 }}

governance:
  max_run_cost_usd: 1.0

graded:
  specs_dir: {root}/datasets/graded-specs
  repeat: 3
  max_cells_per_suite: 8
  holdout: 0.25

observability:
  runs_dir: {root}/outputs/runs
  history_dir: {root}/outputs/history
"""
    config_path = root / "eval_config.yaml"
    config_path.write_text(cfg)
    monkeypatch.setenv("EVAL_CONFIG", str(config_path))
    config.load(force_reload=True)
    yield SimpleNamespace(root=root, config_path=config_path)
    monkeypatch.delenv("EVAL_CONFIG")
    config.load(force_reload=True)


def _drop_prompt(root: Path, name="my-helper.md",
                 text="Greet the user warmly, then answer.\n") -> Path:
    p = root / "input" / name
    p.write_text(text)
    return p


def _drop_bundle(root: Path) -> Path:
    b = root / "input" / "tickets"
    (b / "ticket-golden").mkdir(parents=True)
    (b / "tickets.md").write_text("Summarize support tickets with a SUMMARY: header.\n")
    (b / "cases.yaml").write_text(yaml.safe_dump([{
        "description": "bundle case 1",
        "vars": {"promptFile": "tickets.md", "golden": "ticket-golden",
                 "probe": "summarize this ticket"},
        "assert": [{"type": "python", "value": "file://checks.py:has_summary_header"}],
    }], sort_keys=False))
    (b / "checks.py").write_text(
        "def has_summary_header(output, context):\n"
        '    ok = "SUMMARY:" in str(output)\n'
        '    return {"pass": ok, "score": 1.0 if ok else 0.0, "reason": "hdr"}\n')
    (b / "rubric.md").write_text(
        "# Rubric: tickets\n## dimension: quality\nWhat it measures: quality\n"
        "- 5: a\n- 4: b\n- 3: c\n- 2: d\n- 1: e\n")
    (b / "ticket-golden/pass.txt").write_text("===== STDOUT =====\nSUMMARY: broken order.\n")
    (b / "ticket-golden/fail.txt").write_text("===== STDOUT =====\nno header here\n")
    return b


# ---------------------------------------------------------------- detect —
def test_detect_single_md_is_prompt(fresh_project):
    p = _drop_prompt(fresh_project.root)
    det = detect.detect(p)
    assert (det.kind, det.default_kind, det.suite_id) == ("prompt", "prompt", "my-helper")


def test_detect_tool_markers_make_it_ambiguous_default_agent(fresh_project):
    p = _drop_prompt(fresh_project.root, "worker.md",
                     "---\nallowed-tools: Read, Write\n---\nEdit files in the workspace.\n")
    det = detect.detect(p)
    assert det.kind is None and det.default_kind == "agent"
    assert any("frontmatter" in r for r in det.reasons)


def test_detect_wrapped_by_name_and_by_frontmatter(fresh_project):
    d = fresh_project.root / "input" / "billing"
    d.mkdir()
    (d / "billing.md").write_text("Answer billing questions.\n")
    (d / "billing-wrapper.md").write_text("---\nx: y\n---\nStay in scope.\n")
    det = detect.detect(d)
    assert det.kind == "wrapped" and det.wrapper.name == "billing-wrapper.md"

    d2 = fresh_project.root / "input" / "pair"
    d2.mkdir()
    (d2 / "main.md").write_text("The prompt.\n")
    (d2 / "boundary.md").write_text("---\nkind: command\n---\nBoundary text.\n")
    det2 = detect.detect(d2)
    assert det2.kind == "wrapped" and det2.wrapper.name == "boundary.md"


def test_detect_folder_named_md_is_prompt_extras_raise_agent_default(fresh_project):
    d = fresh_project.root / "input" / "doc-agent"
    d.mkdir()
    (d / "doc-agent.md").write_text("---\ntools: Read, Write\n---\nWrite a summary file.\n")
    (d / "notes.md").write_text("support notes, not a wrapper\n")
    det = detect.detect(d)
    assert det.kind is None and det.default_kind == "agent"
    assert det.prompt.name == "doc-agent.md"
    assert [e.name for e in det.extras] == ["notes.md"]


def test_detect_bundle_and_manifest_override(fresh_project):
    b = _drop_bundle(fresh_project.root)
    det = detect.detect(b)
    assert det.kind == "bundle"

    d = fresh_project.root / "input" / "pinned"
    d.mkdir()
    (d / "pinned.md").write_text("A prompt.\n")
    (d / "input.yaml").write_text("type: agent\nsuite: custom-id\nallowedTools: Read\n")
    det2 = detect.detect(d)
    assert (det2.kind, det2.suite_id) == ("agent", "custom-id")
    assert det2.manifest["allowedTools"] == "Read"


# ----------------------------------------------------- end-to-end per type —
def test_init_prompt_scaffold_validates_end_to_end(fresh_project):
    """The valid-on-arrival guarantee, all the way through cmd_validate
    (three offline promptfoo sweeps) — the one deliberately slow test."""
    from src.runner import cmd_init, cmd_validate
    _drop_prompt(fresh_project.root)
    assert cmd_init(_ns()) == 0
    errors, warnings = checks.run_checks()
    assert errors == []
    assert any(checks.PLACEHOLDER in w for w in warnings)
    assert cmd_validate(argparse.Namespace(suite=None)) == 0
    # the drop box emptied
    assert not (fresh_project.root / "input" / "my-helper.md").exists()
    # ledger written
    assert list((fresh_project.root / "outputs/history").glob("init-*.json"))


def test_init_agent_scaffold(fresh_project):
    from src.runner import cmd_init
    d = fresh_project.root / "input" / "doc-agent"
    d.mkdir()
    (d / "doc-agent.md").write_text("---\nallowed-tools: Read, Write\n---\nWrite files.\n")
    (d / "notes.md").write_text("workspace notes\n")
    assert cmd_init(_ns()) == 0  # --yes accepts the agent default
    dataset = yaml.safe_load((fresh_project.root / "datasets/doc-agent.yaml").read_text())
    assert dataset[0]["vars"]["allowedTools"] == "Read,Write"
    assert dataset[0]["vars"]["fixture"] == "doc-agent-workspace"
    assert (fresh_project.root / "fixtures/doc-agent-workspace/notes.md").exists()
    assert "===== FILE:" in (fresh_project.root
                             / "fixtures/golden/doc-agent/pass.txt").read_text()
    assert checks.run_checks()[0] == []


def test_init_wrapped_scaffold(fresh_project):
    from src.runner import cmd_init
    d = fresh_project.root / "input" / "billing"
    d.mkdir()
    (d / "billing.md").write_text("Answer billing questions.\n")
    (d / "billing-wrapper.md").write_text("---\nx: y\n---\nStay inside billing scope.\n")
    assert cmd_init(_ns()) == 0
    cfg = yaml.safe_load(fresh_project.config_path.read_text())
    assert cfg["prompts"]["wrappers"]["map"] == {"billing.md": "billing-wrapper.md"}
    assert (register.wrappers_dir() / "billing-wrapper.md").exists()
    assert checks.run_checks()[0] == []


def test_init_bundle_moves_and_rewrites_refs(fresh_project):
    from src.runner import cmd_init
    _drop_bundle(fresh_project.root)
    assert cmd_init(_ns()) == 0
    moved = (fresh_project.root / "datasets/tickets.yaml").read_text()
    assert "file://checks.py" not in moved  # ref rewritten to the moved module
    assert "project/checks.py:has_summary_header" in moved
    assert (register.project_root() / "project/checks.py").exists()
    assert (fresh_project.root / "fixtures/golden/ticket-golden/pass.txt").exists()
    assert checks.run_checks()[0] == []
    # bundle content is real, not placeholder: round shows no scaffold line
    assert checks.placeholder_files(config.suite_by_id("tickets")) == []


def test_init_bad_bundle_collects_all_errors_moves_nothing(fresh_project):
    from src.runner import cmd_init
    b = fresh_project.root / "input" / "broken"
    b.mkdir()
    (b / "broken.md").write_text("A prompt.\n")
    (b / "cases.yaml").write_text(yaml.safe_dump([{
        "vars": {"promptFile": "other.md",
                 "golden": "nope", "listy": ["a", "b"]},
        "assert": [{"type": "python", "value": "file://missing.py:fn"}],
    }]))
    rc = cmd_init(_ns(path=str(b), as_kind="bundle"))
    assert rc == 1
    assert (b / "broken.md").exists()          # nothing moved
    assert (b / "cases.yaml").exists()
    assert config.suite_by_id("broken") is None


# ------------------------------------------------------------ config edit —
def test_config_edit_preserves_every_comment_byte(fresh_project):
    before = fresh_project.config_path.read_text()
    comments = [ln for ln in before.split("\n") if ln.strip().startswith("#")]
    result = config_edit.apply(fresh_project.config_path, [
        config_edit.suite_entry_op("s1", "datasets/s1.yaml", "judges/s1.md"),
        config_edit.asserts_file_op("project/asserts.py"),
    ])
    assert result.applied and result.backup.exists()
    after = fresh_project.config_path.read_text()
    for comment in comments:
        if "asserts_file" in comment:
            continue  # deliberately replaced by the uncommenting op
        assert comment in after, f"comment lost: {comment!r}"
    cfg = yaml.safe_load(after)
    assert cfg["suites"] == [{"id": "s1", "file": "datasets/s1.yaml", "rubric": "judges/s1.md"}]
    assert cfg["project"]["asserts_file"] == "project/asserts.py"
    assert result.backup.read_text() == before


def test_config_edit_refuses_and_reports_snippet_when_semantics_break(fresh_project):
    before = fresh_project.config_path.read_text()
    bad = config_edit.Op(
        "sabotage", lambda text: text + "\nsuites: {oops: 1}\n",
        lambda cfg: None, snippet="paste-me")
    result = config_edit.apply(fresh_project.config_path, [bad])
    assert not result.applied
    assert "paste-me" in result.snippet
    assert fresh_project.config_path.read_text() == before  # untouched


def test_appending_second_suite_keeps_the_first(fresh_project):
    config_edit.apply(fresh_project.config_path,
                      [config_edit.suite_entry_op("s1", "datasets/s1.yaml", None)])
    config_edit.apply(fresh_project.config_path,
                      [config_edit.suite_entry_op("s2", "datasets/s2.yaml", None)])
    cfg = yaml.safe_load(fresh_project.config_path.read_text())
    assert [s["id"] for s in cfg["suites"]] == ["s1", "s2"]


# ------------------------------------------------- idempotency / collision —
def test_rerun_is_idempotent_and_collision_is_hard_error(fresh_project):
    from src.runner import cmd_init
    _drop_prompt(fresh_project.root)
    assert cmd_init(_ns()) == 0
    suites_before = yaml.safe_load(fresh_project.config_path.read_text())["suites"]

    _drop_prompt(fresh_project.root)  # identical content again
    assert cmd_init(_ns()) == 0
    suites_after = yaml.safe_load(fresh_project.config_path.read_text())["suites"]
    assert suites_before == suites_after  # no duplicate declaration

    _drop_prompt(fresh_project.root, text="ENTIRELY different prompt\n")
    assert cmd_init(_ns()) == 1  # collision refused


def test_dry_run_changes_nothing(fresh_project):
    from src.runner import cmd_init
    p = _drop_prompt(fresh_project.root)
    before = fresh_project.config_path.read_text()
    assert cmd_init(_ns(dry_run=True)) == 0
    assert p.exists()
    assert fresh_project.config_path.read_text() == before
    assert not (fresh_project.root / "datasets/my-helper.yaml").exists()


def test_ambiguous_nontty_without_yes_refuses(fresh_project, monkeypatch, capsys):
    from src.runner import cmd_init
    _drop_prompt(fresh_project.root, "worker.md",
                 "---\nallowed-tools: Read, Write\n---\nUse the workspace.\n")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert cmd_init(_ns(yes=False)) == 1
    assert "--as agent" in capsys.readouterr().err


# ---------------------------------------------------------- preflight fix —
def test_preflight_zero_suites_exits_with_init_hint(fresh_project, monkeypatch, capsys):
    from src.runner import cmd_preflight
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-branch-test")
    rc = cmd_preflight(argparse.Namespace(suite=None, max_cost=None, force=False))
    assert rc == 1
    assert "prompt-eval init" in capsys.readouterr().err


def test_preflight_defaults_to_first_configured_suite(fresh_project, monkeypatch):
    from src import runner
    from src.runner import cmd_init, cmd_preflight
    _drop_prompt(fresh_project.root)
    assert cmd_init(_ns()) == 0
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-branch-test")
    seen = {}

    def fake_run_live(mode, ns):
        seen["suite"] = ns.suite
        return 1  # stop before the judge smoke — no call paths exercised

    monkeypatch.setattr(runner, "_run_live", fake_run_live)
    assert cmd_preflight(argparse.Namespace(suite=None, max_cost=None, force=False)) == 1
    assert seen["suite"] == "my-helper"


# -------------------------------------------------------------- round line —
def test_round_shows_scaffolded_placeholder_line(fresh_project, capsys):
    from src.runner import cmd_init, cmd_round
    _drop_prompt(fresh_project.root)
    assert cmd_init(_ns()) == 0
    capsys.readouterr()
    assert cmd_round(argparse.Namespace(suite=None)) == 0
    out = capsys.readouterr().out
    assert "scaffolded (placeholders remain" in out


def test_round_zero_suites_points_at_init(fresh_project, capsys):
    from src.runner import cmd_round
    assert cmd_round(argparse.Namespace(suite=None)) == 0
    assert "prompt-eval init" in capsys.readouterr().out

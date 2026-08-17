"""E13: reusable contract helpers + template-variable fairness checks."""

from src import config, contracts_lib
from src.reports import checks

CTX = lambda **v: {"vars": v}  # noqa: E731


def test_json_valid():
    assert contracts_lib.json_valid('{"a": 1}', CTX())["pass"] is True
    assert contracts_lib.json_valid('```json\n{"a": 1}\n```', CTX())["pass"] is True
    r = contracts_lib.json_valid("not json", CTX())
    assert r["pass"] is False and r["reason"].startswith("[format]")


def test_required_headings():
    out = "# Intro\n\ntext\n\n## Next Steps\nmore"
    ok = contracts_lib.required_headings(out, CTX(required_headings="Intro, Next Steps"))
    assert ok["pass"] is True
    r = contracts_lib.required_headings(out, CTX(required_headings="Refund Policy"))
    assert r["pass"] is False and "[missing-content]" in r["reason"]
    assert contracts_lib.required_headings(out, CTX())["pass"] is False  # vacuous check fails


def test_length_between():
    assert contracts_lib.length_between("x" * 50, CTX(min_chars="10", max_chars="100"))["pass"] is True
    assert contracts_lib.length_between("x", CTX(min_chars="10"))["pass"] is False
    assert contracts_lib.length_between("x" * 200, CTX(max_chars="100"))["pass"] is False
    assert contracts_lib.length_between("x", CTX())["pass"] is False


def test_forbidden_phrases():
    r = contracts_lib.forbidden_phrases("We GUARANTEE a refund", CTX(forbidden_phrases="guarantee, asap"))
    assert r["pass"] is False and "[instruction-miss]" in r["reason"]
    assert contracts_lib.forbidden_phrases("polite reply", CTX(forbidden_phrases="guarantee"))["pass"] is True


def test_regex_required():
    assert contracts_lib.regex_required("Order #118", CTX(regex_required=r"#\d+"))["pass"] is True
    assert contracts_lib.regex_required("no number", CTX(regex_required=r"#\d+"))["pass"] is False


# ------------------------------------- template-variable fairness (checks) —
def test_missing_template_var_is_an_error(synthetic_project):
    prod = synthetic_project.root / "prompts/production/demo.md"
    prod.write_text("Greet {{user_name}} warmly, then answer.")
    errors, warnings = checks.run_checks()
    assert any("{{user_name}}" in e and "no such var" in e for e in errors)


def test_declared_template_var_passes(synthetic_project):
    import yaml
    prod = synthetic_project.root / "prompts/production/demo.md"
    prod.write_text("Greet {{user_name}} warmly.")
    ds = synthetic_project.root / "datasets/demo-suite.yaml"
    cases = yaml.safe_load(ds.read_text())
    for c in cases:
        c["vars"]["user_name"] = "Sam"
    ds.write_text(yaml.safe_dump(cases, sort_keys=False))
    errors, warnings = checks.run_checks()
    assert not any("user_name" in e for e in errors)


def test_asymmetric_candidate_template_is_an_error(synthetic_project):
    prod = synthetic_project.root / "prompts/production/demo.md"
    prod.write_text("Greet the user warmly.")
    cand = synthetic_project.root / "prompts/candidates/demo.md"
    cand.write_text("Greet the user warmly. Context: {{secret_context}}")
    errors, warnings = checks.run_checks()
    assert any("different vars" in e and "secret_context" in e for e in errors)


def test_nunjucks_logic_block_warns(synthetic_project):
    prod = synthetic_project.root / "prompts/production/demo.md"
    prod.write_text("Greet.\n{% if mood %}be happy{% endif %}")
    errors, warnings = checks.run_checks()
    assert any("logic blocks" in w for w in warnings)


# ---------------------------------------- E14: file:// prompt sources —
def test_py_const_prompt_source_resolves_and_fingerprints(synthetic_project):
    from src.loaders import pf_prompts
    root = synthetic_project.root
    (root / "myprompts.py").write_text('SYSTEM = "You are a careful assistant."\n')
    ref = f"file://{root}/myprompts.py:SYSTEM"
    text = pf_prompts.resolve_prompt_source(root / "prompts/production", ref)
    assert text == "You are a careful assistant."
    sha1 = pf_prompts.prompt_sha(root / "prompts/production", ref)
    (root / "myprompts.py").write_text('SYSTEM = "You are a DIFFERENT assistant."\n')
    sha2 = pf_prompts.prompt_sha(root / "prompts/production", ref)
    assert sha1 and sha2 and sha1 != sha2  # fingerprint follows the RESOLVED text


def test_py_const_source_errors(synthetic_project):
    import pytest
    from src.loaders import pf_prompts
    root = synthetic_project.root
    with pytest.raises(FileNotFoundError, match="module missing"):
        pf_prompts.resolve_prompt_source(root, "file://nope.py:X")
    (root / "notstr.py").write_text("X = 42\n")
    with pytest.raises(ValueError, match="not a module-level string"):
        pf_prompts.resolve_prompt_source(root, f"file://{root}/notstr.py:X")
    assert pf_prompts.prompt_sha(root, "file://nope.py:X") is None


def test_py_const_source_has_no_candidate_arm(synthetic_project):
    import pytest
    from src.loaders import pf_prompts
    with pytest.raises(ValueError, match="no candidate arm"):
        pf_prompts.candidate_prompt({"vars": {"promptFile": "file://x.py:P"}})


def test_plain_file_fingerprint_stays_byte_sha(synthetic_project):
    from src import state
    from src.loaders import pf_prompts
    d = synthetic_project.root / "prompts/production"
    assert pf_prompts.prompt_sha(d, "demo.md") == state.sha256_file(d / "demo.md")

"""Release-artifact hygiene: the Docker context must never contain the live
key, and the paid PR-workflow example must stay an example — outside
.github/workflows and behind its explicit opt-in label."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_excludes_the_env_file():
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]
    assert ".env" in lines, ".env missing from .dockerignore — a live key could bake into a layer"
    assert ".env.*" in lines, ".env.* missing from .dockerignore — a stray .env.local could bake into a layer"
    # Docker's matcher is root-anchored per segment (no gitignore-style implicit
    # recursion): without **/ a nested input/<bundle>/.env.local survives the
    # filter and COPY . . bakes it into a layer.
    assert "**/.env" in lines and "**/.env.*" in lines, (
        "**/.env patterns missing from .dockerignore — nested env files would enter the build context")


def test_dockerfile_defaults_to_doctor_and_never_copies_env():
    text = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["doctor"]' in text
    assert "COPY .env" not in text


def test_paid_workflow_example_is_not_installed():
    workflows = {p.name for p in (ROOT / ".github" / "workflows").iterdir()}
    assert "prompt-change-pr.yml" not in workflows, (
        "the paid PR workflow is an EXAMPLE — installed here it would spend on this repo")
    assert workflows == {"ci.yml"}


def test_paid_workflow_example_is_label_gated_and_capped():
    text = (ROOT / "examples" / "ci" / "prompt-change-pr.yml").read_text()
    assert "run-paid-eval" in text            # explicit human opt-in
    assert "--max-cost" in text               # explicit ceiling
    assert "SPENDS MONEY" in text             # the loud banner


def test_paid_workflow_key_is_step_scoped():
    """npm ci / uv sync execute PR-controlled lockfiles; a job-level key grant
    would hand the credential to that code. The key belongs only on the steps
    that spend — and the free dry run needs no key at all."""
    import yaml
    wf = yaml.safe_load((ROOT / "examples" / "ci" / "prompt-change-pr.yml").read_text())
    for name, job in wf["jobs"].items():
        job_env = job.get("env") or {}
        assert "secrets." not in str(job_env.get("ANTHROPIC_API_KEY", "")), (
            f"{name}: ANTHROPIC_API_KEY must be step-scoped, never job-level env")
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            step_env = str(step.get("env") or {})
            if "npm ci" in run or "uv sync" in run:
                assert "secrets." not in step_env, (
                    f"{name}: dependency-install steps must never see the key")
            if "--dry-run" in run:
                assert "ANTHROPIC_API_KEY" not in step_env, (
                    f"{name}: the dry run is local arithmetic — no key")

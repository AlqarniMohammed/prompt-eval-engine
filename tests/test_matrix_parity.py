"""Matrix generation is deterministic and structurally sound: same spec in,
identical cells out; every axis value a plain string var (never a YAML array
— the cartesian trap); the holdout fraction reserved and tagged."""

import yaml

from src import config
from src.science.gen_matrix import generate


def test_generation_is_deterministic_and_well_formed(synthetic_project, tmp_path):
    spec = config.specs_dir() / "demo-suite.yaml"
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    generate(spec, out_dir=a_dir)
    generate(spec, out_dir=b_dir)
    a = (a_dir / "demo-suite.yaml").read_text()
    assert a == (b_dir / "demo-suite.yaml").read_text(), "regeneration must be byte-identical"

    cells = yaml.safe_load(a)
    assert len(cells) == 4  # 2x2 axes, under max_cells_per_suite
    for c in cells:
        assert c.get("description")
        for key, value in (c.get("vars") or {}).items():
            assert not isinstance(value, list), f'var "{key}" is a YAML array'
    holdout = [c for c in cells if str(c["vars"].get("holdout")) == "true"]
    assert len(holdout) == 1  # holdout: 0.25 of 4 cells


def test_holdout_selection_is_hash_ranked(synthetic_project, tmp_path):
    """Locks the reservation algorithm, not just the count: holdout is the
    first round(n*fraction) cells ranked by sha256 of the full cell id — not
    a positional stride, which always reserved the last corner of the axes."""
    import hashlib
    spec = config.specs_dir() / "demo-suite.yaml"
    generate(spec, out_dir=tmp_path)
    cells = yaml.safe_load((tmp_path / "demo-suite.yaml").read_text())
    ids = [c["vars"]["cell"] for c in cells]
    expected = set(sorted(ids, key=lambda cid: hashlib.sha256(cid.encode()).hexdigest())[:1])
    tagged = {c["vars"]["cell"] for c in cells if str(c["vars"]["holdout"]) == "true"}
    assert tagged == expected


def test_generated_cells_load_through_dataset_loader(synthetic_project):
    from src.utils.dataset_loader import load_graded_cases
    spec = config.specs_dir() / "demo-suite.yaml"
    generate(spec)  # default out_dir = config.graded_dir()
    cases = load_graded_cases("demo-suite")
    assert cases and all(c["vars"]["suite"] == "demo-suite" for c in cases)
    assert all(str(c["vars"].get("holdout")) != "true" for c in cases), \
        "holdout cells are excluded from iteration runs by default"

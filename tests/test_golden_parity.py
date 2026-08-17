"""The free parity proof, run against the synthetic project: every case's
golden PASS bundles must satisfy every one of its python asserts, and every
golden FAIL bundle must trip at least one. The same guarantee the offline
gate gives a real project — proven here with engine-only machinery."""

import re

from src import config
from src.providers.mock_golden import golden_files
from src.reports.checks import load_asserts_module
from src.utils.dataset_loader import load_cases

_REF = re.compile(r"file://(.+\.py):(\w+)")


def _python_asserts(case: dict):
    for a in case.get("assert") or []:
        if a.get("type") != "python":
            continue
        m = _REF.match(str(a.get("value", "")))
        if m:
            yield m.group(2), getattr(load_asserts_module(m.group(1)), m.group(2))


def _cases_with_goldens():
    out = []
    for case in load_cases():
        golden = case["vars"].get("golden")
        if golden:
            out.append((case, config.fixtures_dir() / "golden" / str(golden)))
    return out


def test_pass_bundles_satisfy_every_assert(synthetic_project):
    cases = _cases_with_goldens()
    assert cases, "synthetic project must declare golden-bearing cases"
    for case, golden_dir in cases:
        files = golden_files(golden_dir, "pass")
        assert files, f"no pass*.txt goldens in {golden_dir}"
        context = {"vars": case["vars"]}
        for name in files:
            bundle = (golden_dir / name).read_text()
            asserts = list(_python_asserts(case))
            assert asserts, "case declares no python asserts"
            for fn_name, fn in asserts:
                result = fn(bundle, context)
                assert result["pass"], f"{name} SHOULD PASS {fn_name}: {result['reason']}"


def test_fail_bundles_trip_at_least_one_assert(synthetic_project):
    for case, golden_dir in _cases_with_goldens():
        files = golden_files(golden_dir, "fail")
        assert files, f"no fail*.txt goldens in {golden_dir}"
        context = {"vars": case["vars"]}
        for name in files:
            bundle = (golden_dir / name).read_text()
            results = [fn(bundle, context) for _, fn in _python_asserts(case)]
            assert any(not r["pass"] for r in results), (
                f"{name} SHOULD FAIL but every deterministic assert passed — "
                "the violating bundle is caught by nothing"
            )

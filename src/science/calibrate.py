"""Judge calibration — the graded analogue of the golden PASS/FAIL gate.
Before any graded run, prove the judge itself over the golden bundles at
k >= calibration.samples per golden, ALL samples in band:
    pass*.txt -> every dimension >= pass_min, every sample
    fail*.txt -> its declared target dimension <= fail_max, every sample
                 (target from the suite's graded spec: calibration.<golden>.failTarget)
    mid*.txt  -> per-sample mean inside (fail_max+0.5 .. pass_min-0.5)
Band misses keep the judge's own words (evidence_on_miss) so re-diagnosis
never needs a paid re-run. Records to
outputs/history/calibration-<suite>-<stamp>.json and the suite state."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src import config, state
from src.utils import fsatomic, redact
from src.evaluators.llm_judge import judge_bundle
from src.providers.mock_golden import golden_files


def _spec_calibration(suite_id: str) -> dict:
    spec = config.specs_dir() / f"{suite_id}.yaml"
    if not spec.exists():
        return {}
    return (yaml.safe_load(spec.read_text()) or {}).get("calibration") or {}


def _goldens_of_suite(suite: dict) -> list[str]:
    cases = yaml.safe_load(config.resolve(suite["file"]).read_text()) or []
    seen: dict[str, None] = {}
    for c in cases:
        g = (c.get("vars") or {}).get("golden")
        if g:
            seen.setdefault(g)
    return list(seen)


def _spread(scores: list[int]) -> str:
    return f"{min(scores)}..{max(scores)}"


def calibrate_suite(suite: dict, log=print) -> dict:
    graded = config.get().get("graded", {})
    cal = graded.get("calibration", {})
    samples_n = int(os.environ.get("EVAL_CAL_SAMPLES") or cal.get("samples", 3))
    pass_min = cal.get("pass_min", 4)
    fail_max = cal.get("fail_max", 2)

    log(f"\n== calibrating judge for {suite['id']} (k={samples_n}, pass>={pass_min}, fail<={fail_max}) ==")
    targets = _spec_calibration(suite["id"])
    record: dict = {"suite": suite["id"], "samples": samples_n, "passMin": pass_min,
                    "failMax": fail_max, "goldens": {}}
    misses: list[str] = []
    judge_model = None
    dim_names: list[str] = []

    def bad(msg: str):
        misses.append(msg)
        log(f"BAND MISS {msg}")

    for golden in _goldens_of_suite(suite):
        gdir = config.fixtures_dir() / "golden" / golden
        for golden_set in ("pass", "fail", "mid"):
            for name in golden_files(gdir, golden_set):
                bundle = (gdir / name).read_text()
                label = f"{golden}/{name}"
                try:
                    samples = [
                        judge_bundle(suite["id"], bundle, {"golden": f"{golden}/{name}#{i}"})
                        for i in range(samples_n)
                    ]
                except RuntimeError as e:
                    # RuntimeError = the judge itself failed (invalid after
                    # re-ask) — go/no-go evidence. Config errors (rubric
                    # parse ValueError etc.) still abort the run.
                    # A judge that can't return valid JSON even after the
                    # re-ask is itself go/no-go evidence — record the miss
                    # and keep sweeping instead of losing the whole run.
                    record["goldens"][label] = {"set": golden_set, "judgeError": str(e)[:300]}
                    bad(f"{label} — judge failed to produce a valid reply: {str(e)[:120]}")
                    continue
                judge_model = samples[0]["judge_model"]
                dim_names = [d["name"] for d in samples[0]["rubric"]["dimensions"]]
                by_dim = {d: [s["scores"][d] for s in samples] for d in dim_names}
                record["goldens"][label] = {"set": golden_set, "byDim": by_dim}
                missed_dims: set[str] = set()

                if golden_set == "pass":
                    for dim, scores in by_dim.items():
                        if any(s < pass_min for s in scores):
                            missed_dims.add(dim)
                            bad(f"{label} — {dim} scored {_spread(scores)} (need every sample >= {pass_min})")
                elif golden_set == "fail":
                    target = (targets.get(golden) or {}).get("failTarget")
                    if target and target not in by_dim:
                        bad(f'{label} — declared failTarget "{target}" is not a rubric dimension')
                    elif target:
                        if any(s > fail_max for s in by_dim[target]):
                            missed_dims.add(target)
                            bad(f"{label} — target {target} scored {_spread(by_dim[target])} "
                                f"(need every sample <= {fail_max})")
                    else:
                        log(f"  WARN {label}: no failTarget declared — requiring ANY dimension <= {fail_max} instead")
                        if not all(any(v <= fail_max for v in s["scores"].values()) for s in samples):
                            missed_dims.update(dim_names)
                            bad(f"{label} — no dimension <= {fail_max} in every sample")
                else:  # mid
                    means = [sum(s["scores"].values()) / len(dim_names) for s in samples]
                    if any(m < fail_max + 0.5 or m > pass_min - 0.5 for m in means):
                        missed_dims.update(dim_names)
                        bad(f"{label} — sample means {', '.join(f'{m:.1f}' for m in means)} outside the mid band")

                if missed_dims:
                    record["goldens"][label]["evidenceOnMiss"] = {
                        d: [s["json"]["dimensions"][d] for s in samples] for d in missed_dims}
                log(f"  {label} [{golden_set}]: "
                    + " · ".join(f"{d} {_spread(s)}" for d, s in by_dim.items()))

    record["judgeModel"] = judge_model
    record["rubricSha256"] = state.sha256_file(config.resolve(suite["rubric"]))
    # The temperature the judge actually ran at — part of the calibration
    # fingerprint, so it must be recorded or the next gate reads it as stale.
    record["judgeTemperature"] = config.agents()["judge"].get("temperature")
    # The PROVIDER-reported concrete model id (dated snapshot) the judge
    # resolved to — providers update models behind stable names, and a
    # calibration is only valid for the snapshot it actually measured.
    record["judgeApiModel"] = _judge_api_model_from_ledger()
    # Self-consistency of the k samples (free — the samples already exist):
    # exact agreement + mean spread, reported so an unstable judge is visible
    # even when every sample lands in band.
    record["consistency"] = consistency_of(record["goldens"])
    if record["consistency"]["pairs"]:
        c = record["consistency"]
        log(f"  self-consistency: exact agreement {c['exactAgreement']:.0%} · "
            f"mean spread {c['meanSpread']:.2f} across {c['pairs']} (golden × dimension) pairs")
    record["green"] = not misses
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = config.history_dir() / f'calibration-{suite["id"]}-{stamp}.json'
    fsatomic.write_text_atomic(out, redact.scrub(json.dumps(record, indent=2))[0])
    log(f"  record: {config.display_path(out)}")

    state.record(suite["id"], "calibrated", {
        "rubric_sha": record["rubricSha256"],
        "judge_model": judge_model,
        "judge_temperature": record["judgeTemperature"],
        "api_model": record["judgeApiModel"],
        "green": record["green"],
        "record": out.name,
    })
    return record


def consistency_of(goldens: dict) -> dict:
    """Self-consistency statistic over a calibration record's per-golden
    byDim score arrays: exactAgreement = fraction of (golden, dimension)
    pairs whose k samples are identical; meanSpread = mean(max - min)."""
    spreads: list[float] = []
    exact = 0
    for g in goldens.values():
        for scores in (g.get("byDim") or {}).values():
            if not scores:
                continue
            spreads.append(max(scores) - min(scores))
            exact += len(set(scores)) == 1
    n = len(spreads)
    return {"pairs": n,
            "exactAgreement": (exact / n) if n else None,
            "meanSpread": (sum(spreads) / n) if n else None}


def _judge_api_model_from_ledger() -> str | None:
    from src.utils import cost_tracker
    rdir = cost_tracker.run_dir()
    if rdir is None:
        return None
    ids = sorted({e.get("apiModel") for e in cost_tracker.read_ledger(rdir)
                  if e.get("kind") == "judge" and e.get("apiModel")})
    return ";".join(ids) if ids else None

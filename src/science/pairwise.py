"""Compare verdict — paired, not threshold-only. Raw score deltas at k=3 sit
inside judge+generation noise, so promotion REQUIRES the blinded pairwise
judge: per matrix cell, the representative (median-by-mean) current and
candidate outputs are judged head-to-head TWICE with the anonymous A/B
labels swapped between calls — a side must win both orders or the cell is a
tie. Threshold deltas (graded.promotion) are documented heuristics
subordinate to the paired verdict.

VERDICT IDEMPOTENCY (incident #8: a REJECT was re-rolled until it passed):
verdicts are keyed on (suite, candidateSha, currentSha, rubricSha,
judgeModel) in outputs/history/verdicts.jsonl. A repeat on an unchanged key
requires a recorded --reason, prints every prior verdict, and a would-be
PROMOTE after a prior REJECT is BLOCKED unless --new-evidence names what
changed (recalibrated judge, more trials, holdout confirmation).

MOCK_PAIRWISE=A|B|tie substitutes a canned pairwise judge (offline proof)."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

from src import config, state
from src.utils import chain, fsatomic, redact
from src.evaluators.llm_judge import judge_call
from src.reports.parse_results import rows_of
from src.utils import rubric as rubric_lib


def _verdicts_path():
    return config.history_dir() / "verdicts.jsonl"


def verdict_key(suite_id: str, current_sha: str | None, candidate_sha: str | None,
                rubric_sha: str | None, judge_model: str) -> str:
    return "|".join(str(x) for x in (suite_id, current_sha, candidate_sha, rubric_sha, judge_model))


def prior_verdicts(key: str) -> list[dict]:
    path = _verdicts_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        if v.get("key") == key:
            out.append(v)
    return out


def _record_verdict(entry: dict):
    # Hash-chained (tamper-evident): see src/utils/chain.py and
    # `prompt-eval history verify`.
    chain.append_chained(_verdicts_path(), entry)


def _is_reject(prior: dict) -> bool:
    """A prior verdict counts as REJECT for the --new-evidence rule only when
    it truly was one. INSUFFICIENT_EVIDENCE is absence of evidence, not
    evidence against — its remedy (a bigger matrix, same content key) must
    not be penalized as a re-roll. Legacy entries have no verdict field."""
    v = prior.get("verdict")
    if v is not None:
        return v == "REJECT"
    return not prior.get("promote")


def _collect_cells(rows: list[dict]) -> list[dict]:
    cells: dict[str, dict] = {}
    for row in rows:
        variables = (row.get("testCase") or {}).get("vars") or row.get("vars") or {}
        variant = variables.get("promptVariant")
        if variant not in ("current", "candidate"):
            continue
        cell_id = variables.get("cell") or variables.get("golden") or "?"
        cell = cells.setdefault(cell_id, {"cell": cell_id, "suite": variables.get("suite"),
                                          "current": [], "candidate": []})
        components = (row.get("gradingResult") or {}).get("componentResults") or []
        judge = next((c for c in components if c.get("namedScores")), None)
        response = row.get("response") or {}
        output = response.get("output")
        cell[variant].append({
            "output": output if isinstance(output, str) else "",
            "scores": judge.get("namedScores") if judge else None,
            "contracts": [
                {"fn": str((c.get("assertion") or {}).get("value", "")).split(":")[-1],
                 "pass": c.get("pass") is not False}
                for c in components
                if ".py:" in str((c.get("assertion") or {}).get("value", ""))
                and "graded_judge" not in str((c.get("assertion") or {}).get("value", ""))
            ],
        })
    return [c for c in cells.values() if c["current"] and c["candidate"]]


def _median_trial(trials: list[dict]) -> dict:
    scored = [t for t in trials if t["scores"]]
    if not scored:
        return trials[0]
    scored.sort(key=lambda t: mean(t["scores"].values()))
    return scored[len(scored) // 2]


def _trial_spread(trials: list[dict]) -> float:
    means = [mean(t["scores"].values()) for t in trials if t["scores"]]
    return max(means) - min(means) if means else 0.0


# Two-sided 95% t critical values by df, normal fallback above 30. A t table
# rather than 1.96 because df here is typically 2-7, where the normal
# interval undercovers badly.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
        13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
        19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
        25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def _sign_test_p(wins_a: int, wins_b: int) -> float | None:
    """Exact two-sided binomial sign test (p0=0.5) over both-order cell wins,
    ties excluded. None when every cell tied (no signal either way)."""
    n = wins_a + wins_b
    if n == 0:
        return None
    k = max(wins_a, wins_b)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)


def _paired_delta_ci95(deltas: list[float]) -> tuple[float, float] | None:
    """95% t-interval on per-cell paired deltas; None below n=2."""
    n = len(deltas)
    if n < 2:
        return None
    m, se = mean(deltas), stdev(deltas) / math.sqrt(n)
    t = _T95.get(n - 1, 1.96)
    return (m - t * se, m + t * se)


def _pairwise_call(suite_id: str, rubric: dict, bundle_a: str, bundle_b: str) -> dict:
    if os.environ.get("MOCK_PAIRWISE"):
        return {"winner": os.environ["MOCK_PAIRWISE"], "reason": "mock"}
    dims = "\n".join(f"- {d['name']}: {d['measures']}" for d in rubric["dimensions"])
    prompt = (
        f'You are comparing two anonymous outputs of the prompt "{suite_id}". '
        f"Judge which output better satisfies this rubric OVERALL:\n{dims}\n\n"
        "Do not reward verbosity. If they are genuinely equivalent, say tie.\n"
        'Return ONLY JSON: {"winner": "A" | "B" | "tie", "reason": "..."}\n\n'
        f"===== OUTPUT A =====\n{bundle_a}\n\n===== OUTPUT B =====\n{bundle_b}"
    )
    for attempt in range(2):
        ask = prompt if attempt == 0 else prompt + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object."
        try:
            text = judge_call(ask)
        except Exception:  # noqa: BLE001 — a failed judge call retries once, then ties
            continue
        try:
            cleaned = re.sub(r"^```[a-z]*\s*\n?|\n?```\s*$", "", str(text).strip(), flags=re.I)
            reply = json.loads(cleaned)
            if reply.get("winner") in ("A", "B", "tie"):
                return reply
        except json.JSONDecodeError:
            continue
    return {"winner": "tie", "reason": "pairwise judge invalid twice — counted as tie (never fabricated)"}


def compare_verdict(results_file, target: str, reason: str | None = None,
                    new_evidence: str | None = None, log=print) -> dict:
    rows = rows_of(results_file)
    cells = _collect_cells(rows)
    if not cells:
        raise ValueError("no compare cells (current+candidate trials) found in results")
    suite_id = cells[0]["suite"]
    rubric = rubric_lib.for_suite(suite_id)
    promotion = config.get().get("graded", {}).get("promotion", {})
    target_gain = promotion.get("target_gain", 0.5)
    max_regression = promotion.get("max_regression", 0.25)
    dims = [d["name"] for d in rubric["dimensions"]]

    def side(label):
        return [t for c in cells for t in c[label] if t["scores"]]

    def dim_means(label):
        return {d: mean(t["scores"][d] for t in side(label)) for d in dims}

    def contract_rate(label, fn=None):
        checks = [x for c in cells for t in c[label] for x in t["contracts"] if not fn or x["fn"] == fn]
        return (sum(1 for x in checks if x["pass"]) / len(checks)) if checks else None

    means_current, means_candidate = dim_means("current"), dim_means("candidate")
    spread = mean(max(_trial_spread(c["current"]), _trial_spread(c["candidate"])) for c in cells)

    # Minimum-evidence gate BEFORE the pairwise loop: an undersized compare
    # costs zero judge calls. 11-9 on a big matrix is noise; 2-0 on a 2-cell
    # matrix is indistinguishable from a coin flip (p = 0.5).
    min_cells = int(promotion.get("min_cells", 3))
    insufficient = len(cells) < min_cells

    wins_current = wins_candidate = ties = 0
    cell_verdicts = []
    if insufficient:
        log(f"INSUFFICIENT EVIDENCE — {len(cells)} compared cell(s) < "
            f"graded.promotion.min_cells {min_cells}; skipping the paid pairwise "
            "calls. Regenerate a larger matrix and re-run compare.")
        ties = len(cells)
    for cell in [] if insufficient else cells:
        a = _median_trial(cell["current"])["output"]
        b = _median_trial(cell["candidate"])["output"]
        r1 = _pairwise_call(suite_id, rubric, a, b)
        r2 = _pairwise_call(suite_id, rubric, b, a)  # swapped
        cand = r1["winner"] == "B" and r2["winner"] == "A"
        curr = r1["winner"] == "A" and r2["winner"] == "B"
        if cand:
            wins_candidate += 1
        elif curr:
            wins_current += 1
        else:
            ties += 1
        cell_verdicts.append({"cell": cell["cell"], "order1": r1["winner"], "order2swapped": r2["winner"],
                              "verdict": "candidate" if cand else "current" if curr else "tie",
                              "reason": r1.get("reason")})

    sign_p = _sign_test_p(wins_candidate, wins_current)
    target_deltas: list[float] = []
    target_ci = None

    if target.startswith("contract:"):
        fn = target[9:]
        rate_a, rate_b = contract_rate("current", fn), contract_rate("candidate", fn)
        target_ok = rate_b is not None and rate_a is not None and rate_b > rate_a and rate_b >= 0.9
        target_desc = f"contract {fn}: {rate_a:.0%} → {rate_b:.0%} (need > and >= 90%)" if rate_a is not None else f"contract {fn}: no data"
    else:
        if target not in dims:
            raise ValueError(f'target "{target}" is not a rubric dimension ({", ".join(dims)})')
        for cell in cells:
            a = [t["scores"][target] for t in cell["current"] if t["scores"]]
            b = [t["scores"][target] for t in cell["candidate"] if t["scores"]]
            if a and b:
                target_deltas.append(mean(b) - mean(a))
        target_ci = _paired_delta_ci95(target_deltas)
        delta = means_candidate[target] - means_current[target]
        target_ok = delta >= target_gain and abs(delta) > spread
        target_desc = (f"dimension {target}: {means_current[target]:.2f} → {means_candidate[target]:.2f} "
                       f"(Δ {delta:+.2f}, need >= +{target_gain} and > spread {spread:.2f})")

    regressions = [d for d in dims if means_candidate[d] - means_current[d] < -max_regression]
    overall_a, overall_b = contract_rate("current"), contract_rate("candidate")
    contract_safe = overall_b is None or overall_a is None or overall_b >= overall_a
    # A candidate that re-breaks a pinned regression case can never promote.
    pinned_failed = sorted({
        str(v.get("cell"))
        for r in rows
        for v in [((r.get("testCase") or {}).get("vars") or {})]
        if v.get("regression") == "true" and v.get("promptVariant") == "candidate"
        and r.get("success") is not True})

    min_wins = int(promotion.get("min_pairwise_wins", 2))
    pairwise_ok = wins_candidate >= min_wins and wins_current == 0
    promote = (pairwise_ok and target_ok and not regressions and contract_safe
               and not insufficient and not pinned_failed)
    # Optional p-gate (graded.promotion.max_sign_p). Absent = reported only,
    # as documented: a default p-gate would make the shipped 6-cell matrix
    # structurally unable to promote. Setting it turns a would-be PROMOTE on
    # thin evidence into INSUFFICIENT_EVIDENCE (opt-in tightening).
    max_sign_p = promotion.get("max_sign_p")
    thin_p = (max_sign_p is not None and promote
              and (sign_p is None or sign_p > float(max_sign_p)))
    if thin_p:
        promote = False

    # ---- idempotency ----
    suite = config.suite_by_id(suite_id)
    from src.loaders.pf_prompts import prompt_sha
    prompt_file = rows[0].get("testCase", {}).get("vars", {}).get("promptFile")
    current_sha = prompt_sha(config.resolve(config.get()["prompts"]["production_dir"]), prompt_file or "")
    candidate_path = config.resolve(config.get()["prompts"].get("candidates_dir", "prompts/candidates")) / (prompt_file or "")
    candidate_sha = state.sha256_file(candidate_path)
    rubric_sha = state.sha256_file(config.resolve(suite["rubric"]))
    judge_model = config.agents()["judge"]["model"]
    key = verdict_key(suite_id, current_sha, candidate_sha, rubric_sha, judge_model)
    priors = prior_verdicts(key)
    blocked = None
    if priors:
        log(f"\n⚠ REPEAT VERDICT — {len(priors)} prior verdict(s) on this exact "
            "(candidate, current, rubric, judge) tuple:")
        for p in priors:
            log(f"  {p['at']}  {'PROMOTE' if p['promote'] else 'REJECT'}"
                + (f"  (reason: {p.get('reason')})" if p.get("reason") else ""))
        if not reason:
            raise PermissionError(
                "re-running a compare on unchanged inputs requires --reason "
                "(what justifies the repeat — e.g. 'harness fix <sha>')")
        if promote and any(_is_reject(p) for p in priors) and not new_evidence:
            blocked = ("BLOCKED: this exact candidate was already REJECTED; a re-rolled "
                       "PROMOTE is not evidence. Re-run with --new-evidence describing what "
                       "changed (recalibrated judge, more trials, holdout confirm) — or revise the candidate.")
            promote = False

    # ---- verdict table ----
    log("─" * 74)
    log(f"COMPARE VERDICT — {suite_id} ({len(cells)} cells, current vs candidate)")
    log("─" * 74)
    log(f"{'dimension':<20} {'current':<9} {'candidate':<9} Δ")
    for d in dims:
        delta = means_candidate[d] - means_current[d]
        log(f"{d:<20} {means_current[d]:<9.2f} {means_candidate[d]:<9.2f} {delta:+.2f}"
            + ("  ⚠ REGRESSION" if d in regressions else ""))
    if overall_a is not None:
        log(f"{'contract pass-rate':<20} {overall_a:<9.0%} {overall_b:<9.0%}")
    log(f"\npairwise (blinded, both orders): candidate wins {wins_candidate} · "
        f"current wins {wins_current} · ties {ties}")
    for v in cell_verdicts:
        log(f"  {v['cell']:<46} {v['order1']}/{v['order2swapped']} → {v['verdict']}")
    log("\nuncertainty (reported, not gated — n is small by design):")
    if sign_p is None:
        log("  sign test on cell wins: all cells tied — no signal either way")
    else:
        n_eff = wins_candidate + wins_current
        log(f"  sign test on cell wins: p = {sign_p:.3f} ({n_eff} non-tie cell{'s' if n_eff != 1 else ''})")
    if target_ci is not None:
        log(f"  target Δ 95% CI (paired per-cell, t, df={len(target_deltas) - 1}): "
            f"[{target_ci[0]:+.2f}, {target_ci[1]:+.2f}]")
    log(f"\ntarget:  {target_desc} {'✓' if target_ok else '✗'}")
    log(f"safety:  regressions {', '.join(regressions) + ' ✗' if regressions else 'none ✓'} · "
        f"contract rate {'safe ✓' if contract_safe else 'REGRESSED ✗'}"
        + (f" · pinned regression cases FAILED ✗ ({', '.join(pinned_failed)})"
           if pinned_failed else ""))
    log(f"pairwise gate: {'✓' if pairwise_ok else '✗'} (need >={min_wins} candidate win-both, 0 current win-both)")
    if blocked:
        log(f"\n{blocked}")
    outcome = ("INSUFFICIENT_EVIDENCE" if (insufficient or thin_p)
               else "PROMOTE" if promote else "REJECT")
    log("─" * 74)
    log(f"VERDICT: {outcome}"
        + (f"  (p = {sign_p if sign_p is not None else 'n/a'} > max_sign_p {max_sign_p})"
           if thin_p else ""))
    log("─" * 74)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    record = {
        "suite": suite_id, "source": str(results_file), "target": target,
        "currentPromptSha256": current_sha, "candidatePromptSha256": candidate_sha,
        "candidateText": redact.durable_text(candidate_path.read_text() if candidate_path.exists() else None),
        "rubricSha256": rubric_sha, "judgeModel": judge_model,
        "dimMeans": {"current": means_current, "candidate": means_candidate},
        "contractRate": {"current": overall_a, "candidate": overall_b},
        "spread": spread, "cellVerdicts": cell_verdicts,
        "pairwise": {"winsCurrent": wins_current, "winsCandidate": wins_candidate, "ties": ties,
                     "minWins": min_wins},
        "stats": {"signTestP": sign_p, "nonTieCells": wins_candidate + wins_current,
                  "targetDeltaCI95": list(target_ci) if target_ci else None,
                  "ciCells": len(target_deltas)},
        "targetOk": target_ok, "regressions": regressions,
        "pinnedRegressionFailures": pinned_failed, "promote": promote,
        "verdict": outcome, "minCells": min_cells,
        "blocked": blocked, "repeatReason": reason, "newEvidence": new_evidence,
    }
    out = config.history_dir() / f"compare-{suite_id}-{stamp}.json"
    fsatomic.write_text_atomic(out, redact.scrub(json.dumps(record, indent=2))[0])
    _record_verdict({"key": key, "at": datetime.now(timezone.utc).isoformat(), "suite": suite_id,
                     "promote": promote, "verdict": outcome,
                     "reason": reason, "newEvidence": new_evidence,
                     "record": out.name,
                     # sha of the fat record file, so the record itself is
                     # tamper-evident through the chained index line
                     "recordSha": state.sha256_file(out)})
    log(f"record: {config.display_path(out)}")

    if _source_run_mode(results_file) == "confirm":
        # A verdict over a confirm run is analysis, not pipeline progress:
        # re-recording "compared" here would wipe the earned "promoted" stage.
        log("note: verdict on a confirm run's results — recorded for analysis; "
            "pipeline state not advanced")
    else:
        state.record(suite_id, "compared", {"verdict": outcome,
                                            "record": out.name, "target": target})
    return record


def _source_run_mode(results_file) -> str | None:
    """Mode of the run that produced a results file, from its manifest."""
    manifest = Path(results_file).parent / "manifest.json"
    try:
        return json.loads(manifest.read_text()).get("mode")
    except (OSError, json.JSONDecodeError):
        name = Path(results_file).parent.name
        return name.split("-", 1)[0] if "-" in name else None

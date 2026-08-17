"""Markdown run report written to <run_dir>/report.md after every run —
the scorecard equivalent, plus the honest trailer: failed runs say FAILED,
findings say findings, spend is measured, exact-cap alerts are surfaced."""

from __future__ import annotations

import json
import re
from pathlib import Path

from src import config
from src.reports.parse_results import rows_of
from src.utils import cost_tracker, fsatomic, redact
from src.utils import status as status_lib


def _row_case(row: dict) -> str:
    tc = row.get("testCase") or {}
    return tc.get("description") or "case"


def write_report(run_dir: Path, manifest: dict) -> Path:
    results_file = run_dir / "results.json"
    rows = rows_of(results_file) if results_file.exists() else []
    entries = cost_tracker.read_ledger(run_dir)
    spend = cost_tracker.summarize([e for e in entries if e["kind"] in ("generation", "judge")])
    alerts = [e for e in entries if e["kind"] == "alert"]
    cap = int(config.agents()["generation"].get("max_tokens_per_turn", 64000))
    cap_hits = cost_tracker.exact_cap_hits(entries, cap)

    failures = [r for r in rows if r.get("success") is not True]
    crashes = [r for r in rows if r.get("error") and not r.get("gradingResult")]
    truncated = [e for e in status_lib.read(run_dir)
                 if e.get("ev") == "case_end" and e.get("truncated")]
    status_line = (
        "FAILED — " + str(manifest.get("killedReason"))
        if manifest.get("killedReason")
        else "FAILED — no result rows" if not rows
        else f"COMPLETED WITH FINDINGS — {len(failures)}/{len(rows)} case-rows have failing asserts"
        if failures else f"COMPLETED CLEAN — {len(rows)}/{len(rows)} case-rows green"
    )

    lines = [
        f"# Run report — {manifest.get('runId')}",
        "",
        f"**{status_line}**",
        "",
        f"- mode: {manifest.get('mode')} · suite: {manifest.get('suite') or 'all'}",
        f"- generation: {manifest.get('models', {}).get('generation')} · "
        f"judge: {manifest.get('models', {}).get('judge')}",
        f"- git: {str(manifest.get('gitSha'))[:12]}{' (dirty)' if manifest.get('gitDirty') else ''}",
        f"- spend: ${spend['totalUsd']:.2f} measured ({spend['calls']} calls: "
        + "  ·  ".join(f"{k} {v['calls']} calls ${v['usd']:.2f}" for k, v in spend["byKind"].items())
        + ")",
    ]
    cache_hits = [e for e in entries if e.get("kind") == "cache_hit"]
    if cache_hits:
        saved = sum(e.get("saved_usd") or 0 for e in cache_hits)
        lines.append(f"- cache: {len(cache_hits)} replay(s), ≈${saved:.2f} generation re-spend avoided")
    if crashes:
        lines.append(f"- ⚠ {len(crashes)} row(s) carry provider errors")
    if cap_hits:
        lines.append(f"- ⚠ {len(cap_hits)} call(s) landed exactly on the {cap}-token cap "
                     "(truncation-retry signature — raise the cap before re-running)")
    for a in alerts:
        lines.append(f"- ⚠ alert: {a.get('note')}")

    if truncated:
        lines += ["", "## Truncated cells", "",
                  "Output cut off by the token limit — these count toward the failure",
                  "breaker and are NEVER retried automatically: re-run explicitly with",
                  "an adjusted limit.", ""]
        lines += [f"- {e.get('case')}" for e in truncated[:50]]

    if failures:
        lines += ["", "## Findings", ""]
        for row in failures[:50]:
            reasons = [
                str(c.get("reason"))
                for c in (row.get("gradingResult") or {}).get("componentResults") or []
                if not c.get("pass")
            ]
            lines.append(f"- **{_row_case(row)}**")
            lines += [f"  - {r}" for r in reasons[:6]]

    tag_counts: dict[str, int] = {}
    for row in failures:
        for c in (row.get("gradingResult") or {}).get("componentResults") or []:
            if c.get("pass"):
                continue
            m = re.match(r"\[([a-z][a-z0-9-]*)\]\s", str(c.get("reason") or ""))
            if m:
                tag_counts[m.group(1)] = tag_counts.get(m.group(1), 0) + 1
    if tag_counts:
        total = sum(tag_counts.values())
        lines += ["", "## Failure taxonomy", "", "| tag | count | share |", "|---|---|---|"]
        lines += [f"| {t} | {n} | {n / total:.0%} |"
                  for t, n in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    scored = [r for r in rows
              for c in [(r.get("gradingResult") or {}).get("componentResults") or []]
              if any(x.get("namedScores") for x in c)]
    if scored:
        dims: dict[str, list[float]] = {}
        for row in scored:
            for c in (row.get("gradingResult") or {}).get("componentResults") or []:
                for k, v in (c.get("namedScores") or {}).items():
                    dims.setdefault(k, []).append(v)
        lines += ["", "## Dimension means", "", "| dimension | mean | n |", "|---|---|---|"]
        lines += [f"| {k} | {sum(v) / len(v):.2f} | {len(v)} |" for k, v in sorted(dims.items())]

    out = run_dir / "report.md"
    out.write_text("\n".join(lines) + "\n")
    return out


RUN_JSON_SCHEMA_VERSION = 1


def write_run_json(run_dir: Path, manifest: dict) -> Path:
    """Consolidated, versioned machine-readable record of one run — the
    single integration point for dashboards and scripts (no joining of
    manifest + results + ledgers required). Written on success AND failure."""
    results_file = run_dir / "results.json"
    rows = rows_of(results_file) if results_file.exists() else []
    entries = cost_tracker.read_ledger(run_dir)
    events = status_lib.read(run_dir)
    truncated_cases = {e.get("case") for e in events
                       if e.get("ev") == "case_end" and e.get("truncated")}
    cache_hits = [e for e in entries if e.get("kind") == "cache_hit"]
    out_rows = []
    for r in rows:
        variables = (r.get("testCase") or {}).get("vars") or {}
        comps = (r.get("gradingResult") or {}).get("componentResults") or []
        judge = next((c for c in comps if c.get("namedScores")), None)
        case_key = variables.get("cell") or (r.get("testCase") or {}).get("description") or "case"
        out_rows.append({
            "case": (r.get("testCase") or {}).get("description"),
            "suite": variables.get("suite"),
            "cell": variables.get("cell"),
            "trial": variables.get("trial"),
            "promptVariant": variables.get("promptVariant"),
            "holdout": variables.get("holdout"),
            "regression": variables.get("regression"),
            "success": r.get("success") is True,
            "scores": judge.get("namedScores") if judge else None,
            "failedReasons": [str(c.get("reason")) for c in comps if not c.get("pass")],
            "truncated": str(case_key) in truncated_cases,
        })
    doc = {
        "schemaVersion": RUN_JSON_SCHEMA_VERSION,
        "manifest": {k: v for k, v in manifest.items() if k != "verbatimConfig"},
        "spend": cost_tracker.summarize(
            [e for e in entries if e.get("kind") in ("generation", "judge", "dataset")]),
        "cache": {"hits": len(cache_hits),
                  "savedUsd": sum(e.get("saved_usd") or 0 for e in cache_hits)},
        "alerts": [e.get("note") for e in entries if e.get("kind") == "alert"],
        "truncatedCells": sorted(str(c) for c in truncated_cases if c),
        "rows": out_rows,
    }
    out = run_dir / "run.json"
    fsatomic.write_text_atomic(out, json.dumps(doc, indent=2))
    return out


def archive_graded(run_dir: Path, manifest: dict):
    """Graded runs append a history record (same schema as the seeded JS-kit
    corpus) so measured_estimates keeps learning."""
    results_file = run_dir / "results.json"
    rows = rows_of(results_file) if results_file.exists() else []
    cases = []
    for row in rows:
        variables = (row.get("testCase") or {}).get("vars") or {}
        judge = next((c for c in (row.get("gradingResult") or {}).get("componentResults") or []
                      if c.get("namedScores")), None)
        cases.append({
            "suite": variables.get("suite"),
            "cell": variables.get("cell"),
            "trial": variables.get("trial"),
            "promptVariant": variables.get("promptVariant"),
            "success": row.get("success") is True,
            "scores": judge.get("namedScores") if judge else None,
        })
    record = {"manifest": manifest, "cases": cases}
    out = config.history_dir() / f"{manifest['runId']}.json"
    fsatomic.write_text_atomic(out, redact.scrub(json.dumps(record, indent=2))[0])
    return out

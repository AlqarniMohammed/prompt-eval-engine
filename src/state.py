"""Per-suite pipeline state machine — sequencing lives in code, not memory
(incident #12: only 2 of 8 documented pipeline links were enforced; a
calibration was never checked before graded runs).

Stages per suite, in order:

    validated -> calibrated -> baselined -> compared -> promoted -> confirmed

Each stage records the content hashes it was earned at; a transition refuses
to run when its predecessor is missing or stale (the hash moved). --force
bypasses a gate but the bypass is recorded in both state and the run
manifest. State lives in outputs/.state.json (gitignored working state; the
durable audit trail is outputs/history/ + per-run manifests).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.utils import fsatomic

STAGES = ["validated", "calibrated", "baselined", "compared", "promoted", "confirmed"]

SNAPSHOT_KEEP = 10


class StateError(Exception):
    pass


def _state_path() -> Path:
    return config.outputs_root() / ".state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load() -> dict:
    path = _state_path()
    if not path.exists():
        return {"suites": {}}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"suites": {}}


def _save(state: dict):
    fsatomic.write_text_atomic(_state_path(), json.dumps(state, indent=2))


def suite_state(suite_id: str) -> dict:
    return load()["suites"].get(suite_id, {})


def _backup_path() -> Path:
    return _state_path().with_name(".state.backup.json")


_snapshotted_this_process = False


def snapshot(auto: bool = False) -> Path:
    """Copy the on-disk working state to the backup file — `graft`'s source.

    Auto-snapshots (from a wiping record) happen at most once per process:
    `validate` re-records suites sequentially, and snapshotting before every
    wipe would leave the backup holding only the last suite un-wiped."""
    global _snapshotted_this_process
    path, bp = _state_path(), _backup_path()
    if auto and _snapshotted_this_process:
        return bp
    if path.exists():
        text = path.read_text()
        fsatomic.write_text_atomic(bp, text)
        _rotate_snapshot(text)
        _snapshotted_this_process = True
    return bp


def _rotate_snapshot(text: str):
    """Keep the last SNAPSHOT_KEEP dated copies beside the single backup file,
    so a bad snapshot can't destroy the only recovery point."""
    snap_dir = _state_path().parent / ".state-snapshots"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    fsatomic.write_text_atomic(snap_dir / f"state-{stamp}.json", text)
    snaps = sorted(snap_dir.glob("state-*.json"))
    for old in snaps[:-SNAPSHOT_KEEP]:
        old.unlink(missing_ok=True)


def record(suite_id: str, stage: str, data: dict):
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage}")
    state = load()
    entry = state["suites"].setdefault(suite_id, {})
    prior = entry.get(stage)
    # Earning an earlier stage invalidates everything after it: a re-validate
    # after prompt surgery must force re-baseline, not resume mid-pipeline.
    # Re-earning it with IDENTICAL content (same shas, same verdict) only
    # refreshes the timestamp — otherwise the free `validate` command, which
    # the runbook says to run at every step, would wipe live calibrations.
    changed = prior is None or {k: v for k, v in prior.items() if k != "at"} != data
    laters = STAGES[STAGES.index(stage) + 1:]
    if changed and any(later in entry for later in laters):
        snapshot(auto=True)  # about to wipe earned stages — keep a graft source
    entry[stage] = {**data, "at": _now()}
    if changed:
        for later in laters:
            entry.pop(later, None)
    _save(state)


def _suite_or_fail(suite_id: str) -> dict:
    suite = config.suite_by_id(suite_id)
    if suite is None:
        known = ", ".join(s["id"] for s in config.suites()) or "none configured"
        raise StateError(f'unknown suite "{suite_id}" (configured suites: {known})')
    return suite


def validate_fingerprint(suite_id: str) -> dict:
    """What 'validated' means for a suite: its dataset, asserts module, the
    engine config, the GENERATED graded matrix (what graded runs actually
    execute), and the pinned regression set. Any of these moving makes the
    validation stale. The matrix + regression keys were added together
    (upgrade note: run the free `validate` once, then `graft` to restore
    calibration/baseline whose own fingerprints still match)."""
    suite = _suite_or_fail(suite_id)
    return {
        "dataset_sha": sha256_file(config.resolve(suite["file"])),
        "asserts_sha": sha256_file(config.asserts_file()),
        "config_sha": sha256_file(config.config_path()),
        "graded_matrix_sha": sha256_file(config.graded_dir() / f"{suite_id}.yaml"),
        "regression_sha": sha256_file(config.regression_dir() / f"{suite_id}.yaml"),
    }


def calibration_fingerprint(suite_id: str) -> dict:
    suite = _suite_or_fail(suite_id)
    return {
        "rubric_sha": sha256_file(config.resolve(suite["rubric"])) if suite.get("rubric") else None,
        "judge_model": config.agents()["judge"]["model"],
        # Sampling changes the judge's behavior as surely as a model swap does;
        # a temperature edit must stale every calibration record.
        "judge_temperature": config.agents()["judge"].get("temperature"),
    }


def smoke_fingerprint(suite_id: str) -> dict:
    """What a smoke run proves: the suite content (validate fingerprint) under
    the current generation + judge models. Any of these moving means the small
    run no longer vouches for the full one."""
    agents = config.agents()
    return {**validate_fingerprint(suite_id),
            "gen_model": agents["generation"]["model"],
            "judge_model": agents["judge"]["model"]}


def record_smoked(suite_id: str, run_id: str):
    """`smoked` is auxiliary state, not a STAGES member: it gates full graded
    runs (subset-first) without adding a box to the pipeline display or the
    successor-wipe order."""
    st = load()
    entry = st["suites"].setdefault(suite_id, {})
    entry["smoked"] = {**smoke_fingerprint(suite_id), "runId": run_id, "at": _now()}
    _save(st)


def check_smoked(suite_id: str) -> list[str]:
    """Subset-first gate: a full graded run requires a successful small run
    first — the '1,000 cases' lesson, enforced in code, not memory."""
    entry = suite_state(suite_id).get("smoked")
    smoke_cmd = f"prompt-eval graded --suite {suite_id} --filter-first-n 2"
    if not entry:
        return [f"suite {suite_id}: no smoke run on record — run a small subset first: {smoke_cmd}"]
    for key, want in smoke_fingerprint(suite_id).items():
        if entry.get(key) != want:
            return [f"suite {suite_id}: smoke run is STALE on {key} — re-run: {smoke_cmd}"]
    return []


def record_preflight(data: dict):
    """Global (suite-independent) preflight record — the real-config smoke.
    Incident #9: a doctor that smoked haiku at 1k tokens couldn't catch a
    streaming-required error on the configured model."""
    st = load()
    st["preflight"] = {**data, "at": _now()}
    _save(st)


def preflight_fingerprint() -> dict:
    gen = config.agents()["generation"]
    return {
        "config_sha": sha256_file(config.config_path()),
        "gen_model": gen["model"],
        "max_tokens_per_turn": gen.get("max_tokens_per_turn"),
    }


def _check_preflight() -> list[str]:
    entry = load().get("preflight")
    if not entry:
        return ["preflight has never been run — `prompt-eval preflight` smokes the real "
                "configuration (~$0.05) before any campaign"]
    problems = []
    for key, want in preflight_fingerprint().items():
        if entry.get(key) != want:
            problems.append(f"preflight is STALE on {key} (recorded {entry.get(key)!r}, current {want!r})")
    return problems


def _check(suite_id: str, stage: str, expected: dict | None, force: bool) -> list[str]:
    entry = suite_state(suite_id).get(stage)
    problems = []
    if not entry:
        problems.append(f'suite {suite_id}: stage "{stage}" has never been recorded')
    elif expected:
        for key, want in expected.items():
            got = entry.get(key)
            if want is not None and got != want:
                problems.append(
                    f'suite {suite_id}: "{stage}" is STALE on {key} '
                    f"(recorded {str(got)[:12]}…, current {str(want)[:12]}…)"
                )
    if stage == "calibrated" and entry and not entry.get("green"):
        problems.append(f"suite {suite_id}: last calibration was NOT green")
    if stage == "calibrated" and entry and entry.get("apiModelDrift"):
        problems.append(
            f"suite {suite_id}: the judge resolved to provider model "
            f"{entry['apiModelDrift']} after calibration ({entry.get('api_model')}) "
            "— the provider updated the model behind its name; recalibrate")
    if problems and force:
        return []  # caller records the forced bypass in the manifest
    return problems


def graft() -> list[str]:
    """Restore wiped stages from the backup snapshot iff their own fingerprints
    still match — honest recovery after a config-only edit invalidated
    `validated` and the re-validate wiped everything downstream. A calibration
    is keyed on {rubric_sha, judge_model} and a baseline on the dataset; when
    neither moved, the paid evidence still holds. Anything whose fingerprint
    moved is refused. Restored entries are stamped `restored_from` for audit."""
    bp = _backup_path()
    if not bp.exists():
        raise StateError(f"no snapshot at {bp} — nothing to graft from")
    backup = json.loads(bp.read_text())
    st = load()
    lines = []
    for suite_id, old in (backup.get("suites") or {}).items():
        entry = st["suites"].setdefault(suite_id, {})
        cal = old.get("calibrated")
        if cal and "calibrated" not in entry:
            want = calibration_fingerprint(suite_id)
            if cal.get("green") and all(cal.get(k) == v for k, v in want.items()):
                entry["calibrated"] = {**cal, "restored_from": bp.name}
                lines.append(f"{suite_id}: calibrated restored")
            else:
                lines.append(f"{suite_id}: calibrated NOT restored (rubric/judge moved, or not green)")
        base = old.get("baselined")
        if base and "baselined" not in entry:
            old_ds = (old.get("validated") or {}).get("dataset_sha")
            if old_ds and old_ds == validate_fingerprint(suite_id)["dataset_sha"] \
                    and "calibrated" in entry:
                entry["baselined"] = {**base, "restored_from": bp.name}
                lines.append(f"{suite_id}: baselined restored")
            else:
                lines.append(f"{suite_id}: baselined NOT restored (dataset moved, or calibration missing)")
    # A preflight can be missing (restore from backup) or present but stale
    # only on config_sha (the config was edited without touching model or
    # caps). Either way: restore/re-stamp iff the load-bearing fields match.
    live_pf = st.get("preflight")
    pf = live_pf or backup.get("preflight")
    want = preflight_fingerprint()
    if pf and (live_pf is None or {k: pf.get(k) for k in want} != want):
        if (pf.get("gen_model") == want["gen_model"]
                and pf.get("max_tokens_per_turn") == want["max_tokens_per_turn"]):
            st["preflight"] = {**pf, "config_sha": want["config_sha"], "restored_from": bp.name}
            lines.append("preflight restored (gen model + caps verified; config_sha re-stamped)")
        else:
            lines.append("preflight NOT restored (gen model or token cap moved)")
    _save(st)
    return lines


def flag_api_model_drift(suite_id: str, resolved_id: str):
    """Stamp drift on the calibrated entry WITHOUT the successor-wipe of
    record() — the run that detected it already happened; the flag gates the
    NEXT run. Cleared naturally by the fresh entry a recalibration records."""
    st = load()
    entry = (st["suites"].get(suite_id) or {}).get("calibrated")
    if entry is not None and entry.get("apiModelDrift") != resolved_id:
        entry["apiModelDrift"] = resolved_id
        _save(st)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _dir_shas(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {f.name: sha256_file(f) for f in directory.iterdir() if f.is_file()}


def rebuild_from_history() -> list[str]:
    """Reconstruct wiped pipeline state from the durable records in
    outputs/history/ — the disaster path when both `.state.json` and its
    backup snapshot are gone (incident #16 was recovered from these records
    by hand; this is that procedure in code).

    Same honesty rule as `graft`: a stage is restored only when the history
    record's own fingerprints still match current disk/config; everything
    else is refused with the reason printed. Restored entries are stamped
    `restored_from: "history"`. Run the free `validate` first — recording
    `validated` afterwards would wipe whatever this restores."""
    hist = config.history_dir()
    if not hist.is_dir():
        raise StateError(f"no history at {hist} — nothing to rebuild from")
    cfg = config.get()
    prod_shas = _dir_shas(config.resolve(cfg["prompts"]["production_dir"]))
    cand_shas = _dir_shas(config.resolve(cfg["prompts"].get("candidates_dir", "prompts/candidates")))
    agents = config.agents()
    st = load()
    lines: list[str] = []
    for suite in config.suites():
        sid = suite["id"]
        entry = st["suites"].setdefault(sid, {})
        if "validated" not in entry:
            lines.append(f"{sid}: SKIPPED — run the free `prompt-eval validate` first "
                         "(recording it later would wipe anything restored now)")
            continue
        _rebuild_calibrated(sid, entry, hist, lines)
        _rebuild_baselined(sid, entry, hist, agents, prod_shas, lines)
        _rebuild_compared(sid, entry, hist, prod_shas, cand_shas, lines)
        _rebuild_promoted(sid, entry, prod_shas, lines)
        _rebuild_confirmed(sid, entry, hist, lines)
    _save(st)
    return lines


def _rebuild_calibrated(sid: str, entry: dict, hist: Path, lines: list[str]):
    if "calibrated" in entry:
        return
    want = calibration_fingerprint(sid)
    for path in sorted(hist.glob(f"calibration-{sid}-*.json"), reverse=True):
        rec = _read_json(path)
        if not rec or not rec.get("green"):
            continue
        if rec.get("judgeModel") != want["judge_model"] or rec.get("rubricSha256") != want["rubric_sha"]:
            continue
        if "judgeTemperature" in rec:
            if rec.get("judgeTemperature") != want["judge_temperature"]:
                continue
        elif want["judge_temperature"] is not None:
            continue  # legacy record can't prove the temperature it ran at
        entry["calibrated"] = {"rubric_sha": want["rubric_sha"],
                               "judge_model": want["judge_model"],
                               "judge_temperature": want["judge_temperature"],
                               "green": True, "record": path.name,
                               "at": _now(), "restored_from": "history"}
        lines.append(f"{sid}: calibrated restored from {path.name}")
        return
    lines.append(f"{sid}: calibrated NOT restored (no green record matching the "
                 "current rubric/judge/temperature)")


def _rebuild_baselined(sid: str, entry: dict, hist: Path, agents: dict,
                       prod_shas: dict, lines: list[str]):
    if "baselined" in entry:
        return
    if "calibrated" not in entry:
        lines.append(f"{sid}: baselined NOT restored (calibration missing)")
        return
    for path in sorted(hist.glob("graded-*.json"), reverse=True):
        rec = _read_json(path)
        if not rec:
            continue
        manifest, cases = rec.get("manifest") or {}, rec.get("cases") or []
        if manifest.get("suite") != sid and not any(c.get("suite") == sid for c in cases):
            continue
        if manifest.get("models") != {"generation": agents["generation"]["model"],
                                      "judge": agents["judge"]["model"]}:
            continue
        shas = manifest.get("promptSha256") or {}
        if not shas or any(prod_shas.get(f) != sha for f, sha in shas.items()):
            continue
        entry["baselined"] = {"runId": manifest.get("runId", path.stem),
                              "at": _now(), "restored_from": "history"}
        lines.append(f"{sid}: baselined restored from {path.name}")
        return
    lines.append(f"{sid}: baselined NOT restored (no graded record matching the "
                 "current prompts/models)")


def _rebuild_compared(sid: str, entry: dict, hist: Path, prod_shas: dict,
                      cand_shas: dict, lines: list[str]):
    if "compared" in entry:
        return
    if "baselined" not in entry:
        lines.append(f"{sid}: compared NOT restored (baseline missing)")
        return
    want = calibration_fingerprint(sid)
    for path in sorted(hist.glob(f"compare-{sid}-*.json"), reverse=True):
        rec = _read_json(path)
        if not rec:
            continue
        if rec.get("rubricSha256") != want["rubric_sha"] or rec.get("judgeModel") != want["judge_model"]:
            continue
        known_shas = set(prod_shas.values()) | set(cand_shas.values())
        if rec.get("currentPromptSha256") not in known_shas \
                or rec.get("candidatePromptSha256") not in known_shas:
            continue
        entry["compared"] = {"verdict": "PROMOTE" if rec.get("promote") else "REJECT",
                             "record": path.name, "target": rec.get("target"),
                             "at": _now(), "restored_from": "history"}
        lines.append(f"{sid}: compared restored from {path.name}")
        return
    lines.append(f"{sid}: compared NOT restored (no verdict record matching the "
                 "current prompts/rubric/judge)")


def _rebuild_promoted(sid: str, entry: dict, prod_shas: dict, lines: list[str]):
    if "promoted" in entry:
        return
    compared = entry.get("compared")
    if not compared or compared.get("verdict") != "PROMOTE":
        return  # nothing to say — no promotion is on record to restore
    record_name = compared.get("record")
    rec = _read_json(config.history_dir() / record_name) if record_name else None
    cand_sha = (rec or {}).get("candidatePromptSha256")
    match = next((f for f, sha in prod_shas.items() if sha and sha == cand_sha), None)
    if match:
        entry["promoted"] = {"promptFile": match, "sha": cand_sha,
                             "compareRecord": record_name,
                             "at": _now(), "restored_from": "history"}
        lines.append(f"{sid}: promoted restored ({match} matches the promoted candidate)")
    else:
        lines.append(f"{sid}: promoted NOT restored (no production prompt matches "
                     "the promoted candidate's sha)")


def _rebuild_confirmed(sid: str, entry: dict, hist: Path, lines: list[str]):
    if "confirmed" in entry or "promoted" not in entry:
        return
    for path in sorted(hist.glob("confirm-*.json"), reverse=True):
        rec = _read_json(path)
        if not rec:
            continue
        cases = [c for c in rec.get("cases") or [] if c.get("suite") == sid]
        if not cases or any(c.get("success") is not True for c in cases):
            continue
        run_id = (rec.get("manifest") or {}).get("runId", path.stem)
        entry["confirmed"] = {"runId": run_id, "rows": len(cases),
                              "at": _now(), "restored_from": "history"}
        lines.append(f"{sid}: confirmed restored from {path.name}")
        return
    lines.append(f"{sid}: confirmed NOT restored (no clean confirm record for this suite)")


def require(suite_id: str, target: str, force: bool = False) -> list[str]:
    """Gate for entering `target`. Returns the list of forced-past problems
    (empty when genuinely clean); raises StateError when not forced."""
    needs: list[tuple[str, dict | None]] = []
    if target in ("baseline", "graded"):
        needs = [("validated", validate_fingerprint(suite_id)),
                 ("calibrated", calibration_fingerprint(suite_id))]
    elif target == "compare":
        needs = [("validated", validate_fingerprint(suite_id)),
                 ("calibrated", calibration_fingerprint(suite_id)),
                 ("baselined", None)]
    elif target == "promote":
        needs = [("compared", None)]
    elif target == "confirm":
        needs = [("promoted", None)]
    elif target == "calibrate":
        needs = [("validated", validate_fingerprint(suite_id))]
    else:
        raise ValueError(f"unknown transition target {target}")

    problems = []
    if target in ("baseline", "graded", "compare", "confirm"):
        problems += _check_preflight()
    for stage, expected in needs:
        problems += _check(suite_id, stage, expected, force=False)
    if problems and not force:
        raise StateError(
            "pipeline gate refused:\n  " + "\n  ".join(problems)
            + "\nRun the missing/stale stage first, or override once with --force "
            "(the bypass is recorded in the manifest)."
        )
    return problems

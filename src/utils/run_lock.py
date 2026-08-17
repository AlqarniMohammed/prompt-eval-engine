"""Single-run mutex for EVERY spending entrypoint (run, graded, compare,
confirm, calibrate, pairwise, preflight). Concurrent generation runs starved
each other into 77-minute timeout marches (incident #1) and the JS lock only
covered run.js — calibrate/pairwise could still collide. A lock whose pid is
dead is stale and reclaimed silently."""

from __future__ import annotations

import atexit
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src import config


class LockHeld(Exception):
    pass


def lock_file() -> Path:
    return config.outputs_root() / ".run.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire(run_id: str):
    """Acquire the run lock or raise LockHeld. Released on process exit;
    returns an explicit release callable."""
    path = lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        held = None
        try:
            held = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass  # corrupt -> stale
        if held and held.get("pid") and _pid_alive(int(held["pid"])):
            raise LockHeld(
                f"another live run holds the lock: {held.get('runId')} "
                f"(pid {held['pid']}, since {held.get('startedAt')}).\n"
                "Spending runs must never overlap. Wait for it to finish, or kill its "
                "WHOLE process group (kill by pid, verify with pgrep) before retrying."
            )
        path.unlink(missing_ok=True)  # stale lock from a dead process
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "runId": run_id,
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }))

    def release():
        try:
            current = json.loads(path.read_text())
            if current.get("pid") == os.getpid():
                path.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            pass

    atexit.register(release)
    return release

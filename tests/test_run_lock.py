import json

import pytest

from src.utils import run_lock


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(run_lock, "lock_file", lambda: tmp_path / ".run.lock")


def test_acquire_release():
    release = run_lock.acquire("run-1")
    assert run_lock.lock_file().exists()
    release()
    assert not run_lock.lock_file().exists()


def test_second_acquire_from_live_pid_raises():
    run_lock.acquire("run-1")
    with pytest.raises(run_lock.LockHeld, match="run-1"):
        run_lock.acquire("run-2")


def test_stale_lock_reclaimed():
    run_lock.lock_file().write_text(json.dumps({"pid": 99999999, "runId": "dead", "startedAt": "x"}))
    release = run_lock.acquire("run-3")
    assert json.loads(run_lock.lock_file().read_text())["runId"] == "run-3"
    release()


def test_corrupt_lock_reclaimed():
    run_lock.lock_file().write_text("{not json")
    release = run_lock.acquire("run-4")
    release()

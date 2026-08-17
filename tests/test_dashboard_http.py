"""Dashboard HTTP layer — a real ThreadingHTTPServer on port 0 in a daemon
thread against the synthetic project: every endpoint answers 200 with its
schema keys, bad run ids 404, writes 405, and the page is self-contained."""

import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from src.dashboard import server as dash_server


@pytest.fixture
def dashboard(synthetic_project):
    httpd = dash_server.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


ENDPOINT_KEYS = {
    "/api/meta": {"project", "models", "governance", "suites", "heartbeatSeconds"},
    "/api/live": {"active", "now"},
    "/api/runs": {"runs"},
    "/api/state": {"suites", "preflight", "stageDocs", "stageOrder"},
    "/api/pipeline": {"stageDocs", "stageOrder"},
    "/api/history": {"series", "spendByMode", "calibrations", "measured"},
    "/api/verdicts": {"verdicts"},
    "/api/evidence": {"records", "histograms", "worst", "suggestedFixes"},
}


def test_every_endpoint_answers_200_with_schema_keys(dashboard):
    for path, keys in ENDPOINT_KEYS.items():
        status, body, ctype = _get(dashboard, path)
        assert status == 200, f"{path} → {status}"
        assert "application/json" in ctype
        payload = json.loads(body)
        missing = keys - set(payload)
        assert not missing, f"{path} missing keys {missing}"


def test_index_served_and_selfcontained(dashboard):
    status, body, ctype = _get(dashboard, "/")
    assert status == 200 and "text/html" in ctype
    html = body.decode()
    # Self-containment guard: no external scripts, styles, images, or fonts.
    assert not re.search(r'\bsrc\s*=\s*["\']\s*(https?:)?//', html)
    assert not re.search(r'\bhref\s*=\s*["\']\s*(https?:)?//', html)
    assert "<script" in html and "</svg>" not in html.split("<script")[0]
    # And it must be the same file the package ships.
    shipped = (Path(dash_server.__file__).parent / "index.html").read_text()
    assert html == shipped


def test_run_detail_endpoint_and_bad_ids_404(dashboard, synthetic_project):
    runs_dir = synthetic_project.root / "outputs/runs" / "graded-x"
    runs_dir.mkdir(parents=True)
    (runs_dir / "manifest.json").write_text(json.dumps({"runId": "graded-x", "mode": "graded"}))
    status, body, _ = _get(dashboard, "/api/run/graded-x")
    assert status == 200 and json.loads(body)["runId"] == "graded-x"

    for bad in ("/api/run/no-such-run", "/api/run/..%2f..%2fsecrets",
                "/api/run/a%2fb", "/api/nothing"):
        status, _, _ = _get(dashboard, bad)
        assert status == 404, f"{bad} → {status}"


def test_non_get_is_405_and_server_never_writes(dashboard, synthetic_project):
    before = sorted(p for p in synthetic_project.root.rglob("*") if p.is_file())
    req = urllib.request.Request(dashboard + "/api/live", data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 405
    for path in ENDPOINT_KEYS:
        _get(dashboard, path)
    after = sorted(p for p in synthetic_project.root.rglob("*") if p.is_file())
    assert before == after  # read-only: not one file created anywhere


def test_torn_artifacts_do_not_500(dashboard, synthetic_project):
    outputs = synthetic_project.root / "outputs"
    (outputs / "runs").mkdir(parents=True, exist_ok=True)
    (outputs / ".run.lock").write_text('{"pid": 12')          # torn lock
    run = outputs / "runs" / "graded-torn"
    run.mkdir()
    (run / "manifest.json").write_text('{"runId": "gr')       # torn manifest
    (outputs / "history").mkdir(exist_ok=True)
    (outputs / "history" / "graded-torn.json").write_text("{")
    for path in ENDPOINT_KEYS:
        status, _, _ = _get(dashboard, path)
        assert status == 200, f"{path} → {status} on torn artifacts"

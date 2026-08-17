"""Thin stdlib HTTP layer for the dashboard.

Read-only by construction: GET-only handler, binds 127.0.0.1 only (run
artifacts contain prompt text), never writes, never sets EVAL_RUN_DIR, never
touches state. No caching — every request re-reads the artifacts fresh, so a
torn read self-heals on the next poll.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src import config
from src.dashboard import data
from src.dashboard.pipeline_docs import STAGE_DOCS, ordered_stages

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_INDEX = Path(__file__).parent / "index.html"


def _meta() -> dict:
    cfg = config.get()
    governance = cfg.get("governance") or {}
    observability = cfg.get("observability") or {}
    return {
        "project": (cfg.get("project") or {}).get("name"),
        "engineVersion": config.ENGINE_VERSION,
        "models": {
            "generation": cfg["agents"]["generation"].get("model"),
            "judge": cfg["agents"]["judge"].get("model"),
            "dataset": (cfg["agents"].get("dataset") or {}).get("model"),
        },
        "productionModel": (cfg.get("project") or {}).get("production_model"),
        "governance": {
            "maxRunCostUsd": governance.get("max_run_cost_usd"),
            "failureBreaker": governance.get("failure_breaker"),
        },
        "heartbeatSeconds": heartbeat_seconds(),
        "suites": [{"id": s["id"], "rubric": bool(s.get("rubric"))} for s in config.suites()],
    }


def heartbeat_seconds() -> float:
    """observability.heartbeat_seconds — the live panel's stall threshold
    (stalled = no activity for 2x this)."""
    raw = (config.get().get("observability") or {}).get("heartbeat_seconds", 30)
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def routes() -> dict:
    """Path -> zero-arg callable returning a JSON-able object. Rebuilt per
    request cheaply; config.load stays cached inside the process."""
    outputs_root = config.outputs_root()
    runs_dir = config.outputs_dir()
    history_dir = config.history_dir()
    ceiling = float(config.get()["governance"]["max_run_cost_usd"])
    return {
        "/api/meta": _meta,
        "/api/live": lambda: data.live_snapshot(outputs_root, runs_dir,
                                                heartbeat_seconds(), ceiling),
        "/api/runs": lambda: {"runs": data.list_runs(runs_dir)},
        "/api/state": lambda: {**data.pipeline_view(), "stageDocs": STAGE_DOCS,
                               "stageOrder": ordered_stages()},
        "/api/pipeline": lambda: {"stageDocs": STAGE_DOCS, "stageOrder": ordered_stages()},
        "/api/history": lambda: {**data.history_series(history_dir),
                                 "measured": data.spend_accounting(runs_dir)},
        "/api/verdicts": lambda: {"verdicts": data.verdict_list(history_dir)},
        "/api/evidence": lambda: data.evidence_summary(
            data.evidence_sources(outputs_root, runs_dir)),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "prompt-eval-dashboard"

    def log_message(self, fmt, *args):  # quiet by default
        pass

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
                return
            table = routes()
            if path in table:
                self._json(200, table[path]())
                return
            m = re.fullmatch(r"/api/run/([^/]+)", path)
            if m:
                run_id = m.group(1)
                runs_dir = config.outputs_dir()
                resolved = (runs_dir / run_id).resolve()
                if not _RUN_ID.fullmatch(run_id) or not resolved.is_relative_to(
                        runs_dir.resolve()):
                    self._json(404, {"error": "no such run"})
                    return
                detail = data.run_detail(runs_dir, run_id)
                self._json(200, detail) if detail else self._json(404, {"error": "no such run"})
                return
            self._json(404, {"error": "no such endpoint"})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — a torn read must never kill the server
            try:
                self._json(500, {"error": str(e)[:300]})
            except OSError:
                pass

    def do_POST(self):  # noqa: N802
        self._json(405, {"error": "read-only dashboard"})

    do_PUT = do_DELETE = do_PATCH = do_POST


def make_server(port: int) -> ThreadingHTTPServer:
    """Bound to 127.0.0.1 only — artifacts contain prompt text."""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int, open_browser: bool = False) -> int:
    try:
        httpd = make_server(port)
    except OSError as e:
        import sys
        print(f"cannot bind 127.0.0.1:{port} ({e.strerror or e}) — "
              f"try another port: prompt-eval dashboard --port {port + 1}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"dashboard  {url}  (read-only · localhost only · Ctrl-C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        httpd.server_close()
    return 0

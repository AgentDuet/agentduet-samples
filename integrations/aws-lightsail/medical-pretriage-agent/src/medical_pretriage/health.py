"""Minimal HTTP health endpoint for Docker probes."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

_ready = False
_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


def set_ready(value: bool) -> None:
    global _ready
    _ready = value


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.debug("health %s - %s", self.address_string(), format % args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            if _ready:
                self._json(200, {"status": "ok"})
            else:
                self._json(503, {"status": "starting"})
            return
        if path == "/live":
            self._json(200, {"status": "alive"})
            return
        self._json(404, {"status": "not_found"})


def start_health_server(host: str, port: int) -> None:
    """Start a daemon HTTP server for readiness probes."""
    global _server, _thread
    if _server is not None:
        return

    _server = ThreadingHTTPServer((host, port), _HealthHandler)
    _thread = threading.Thread(
        target=_server.serve_forever,
        name="health-http",
        daemon=True,
    )
    _thread.start()
    logger.info("Health server listening on http://%s:%s/health", host, port)


def stop_health_server() -> None:
    global _server, _thread
    if _server is None:
        return
    _server.shutdown()
    _server.server_close()
    _server = None
    _thread = None

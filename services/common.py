"""What every service shares: health endpoints, graceful shutdown, request ids, JSON logs.

Standard library only, so the container image needs nothing installed.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DownstreamError(Exception):
    """The service we called is unreachable, timed out, or returned a server error."""


def call(url: str, request_id: str, timeout: float = 2.0) -> tuple[int, dict]:
    """GET a JSON document from another service, passing the request id along.

    Returns (status, body) for 2xx and 4xx. Raises DownstreamError when the
    other side is down or broken, so callers must decide what to do about it.
    """
    request = urllib.request.Request(url, headers={"X-Request-Id": request_id, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise DownstreamError(f"{url} returned {exc.code}") from exc
        return exc.code, json.loads(exc.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
        raise DownstreamError(f"{url}: {exc}") from exc


class Service:
    """A small HTTP server with the behaviour Kubernetes expects from a pod.

    /healthz  liveness: the process is running and can answer. Never checks
              dependencies, or one bad database would restart every pod.
    /readyz   readiness: 200 while serving, 503 once shutdown has begun, so
              the pod is taken out of the Service before it stops.
    SIGTERM   stop accepting new connections, finish requests in flight, exit 0.
    """

    def __init__(self, name: str, routes: dict, port: int = 0, log=sys.stdout) -> None:
        self.name, self.routes, self.log = name, routes, log
        self.draining = threading.Event()
        self.in_flight = 0
        self._lock = threading.Lock()
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                started = time.monotonic()
                request_id = self.headers.get("X-Request-Id") or uuid.uuid4().hex
                with service._lock:
                    service.in_flight += 1
                try:
                    status, body = service.dispatch(self.path.split("?", 1)[0], request_id)
                except DownstreamError as exc:
                    status, body = 502, {"error": "upstream unavailable", "detail": str(exc)}
                except Exception as exc:                        # a bug in a handler must not kill the connection silently
                    status, body = 500, {"error": "internal error", "detail": type(exc).__name__}
                finally:
                    with service._lock:
                        service.in_flight -= 1
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Request-Id", request_id)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True
                if self.path not in ("/healthz", "/readyz"):   # probes would drown out real traffic in the logs
                    service.write_log(request_id=request_id, path=self.path, status=status,
                                      ms=round((time.monotonic() - started) * 1000, 1))

            def log_message(self, *args) -> None:               # replace the default plain-text access log
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.server.daemon_threads = False                      # with block_on_close, shutdown waits for open requests
        self.port = self.server.server_address[1]

    def dispatch(self, path: str, request_id: str) -> tuple[int, dict]:
        if path == "/healthz":
            return 200, {"status": "ok", "service": self.name}
        if path == "/readyz":
            return (503, {"status": "draining"}) if self.draining.is_set() else (200, {"status": "ready"})
        for prefix, handler in self.routes.items():
            # "/orders/" is a prefix route and receives the rest of the path; "/" matches only itself.
            if path == prefix or (len(prefix) > 1 and prefix.endswith("/") and path.startswith(prefix)):
                return handler(path[len(prefix):] if len(prefix) > 1 else "", request_id)
        return 404, {"error": "not found", "path": path}

    def write_log(self, **fields) -> None:
        self.log.write(json.dumps({"ts": round(time.time(), 3), "service": self.name, **fields}) + "\n")
        self.log.flush()

    def start(self) -> "Service":
        """Serve in a background thread (used by tests)."""
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Graceful stop: fail readiness, stop accepting, wait for requests in flight."""
        self.draining.set()
        self.server.shutdown()
        self.server.server_close()

    def run(self) -> None:
        """Serve until SIGTERM or SIGINT, then drain and return."""
        def on_signal(signum, frame):
            self.write_log(event="shutdown requested", signal=signal.Signals(signum).name, in_flight=self.in_flight)
            self.draining.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()   # shutdown() must not run on the serving thread

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        self.write_log(event="listening", port=self.port)
        self.server.serve_forever(poll_interval=0.1)
        self.server.server_close()
        self.write_log(event="stopped cleanly")


def env_port() -> int:
    return int(os.environ.get("PORT", "8080"))

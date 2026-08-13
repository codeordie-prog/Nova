"""
Cloud Run health wrapper for long-running worker containers.

Cloud Run requires the container to listen on ``PORT`` for startup/liveness probes.
Background workers (Redis consumers) do not expose HTTP by default; this script
starts a minimal ``/health`` listener in a daemon thread, then runs the worker command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable


def _start_health_server(*, host: str, port: int) -> HTTPServer:
    """
    Start a background HTTP server exposing GET / and GET /health.

    Args:
        host: Bind address.
        port: Listen port (Cloud Run sets ``PORT``).

    Returns:
        HTTPServer: Started server instance (daemon thread serves forever).
    """

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in {"/", "/health"}:
                body = json.dumps({"status": "ok"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer((host, port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_with_health(main: Callable[[], None]) -> None:
    """
    Run ``main`` after binding the Cloud Run health port.

    Args:
        main: Callable that blocks for the lifetime of the worker process.

    Raises:
        SystemExit: When ``main`` returns or the worker subprocess exits non-zero.
    """
    port = int(os.environ.get("PORT", "8080"))
    _start_health_server(host="0.0.0.0", port=port)
    main()


def _run_subprocess(argv: list[str]) -> None:
    """
    Execute a worker command and propagate its exit code.

    Args:
        argv: Command argv (program + args).

    Raises:
        SystemExit: With the child process exit code when non-zero.
    """
    if not argv:
        raise SystemExit("run_with_health.py: missing worker command")
    completed = subprocess.run(argv, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    """
    CLI entrypoint: ``python deploy/scripts/run_with_health.py <cmd> [args...]``.

    Raises:
        SystemExit: On usage error or worker failure.
    """
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_with_health.py <command> [args...]")
    argv = sys.argv[1:]
    run_with_health(lambda: _run_subprocess(argv))


if __name__ == "__main__":
    main()

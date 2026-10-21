"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-30
License: MIT

Cloud Run workers entrypoint: health on ``PORT``, optional Turn sidecar on :8000, then ``do_work.py``.

The outbound worker expects ``WHATSAPP_API_BASE_URL=http://127.0.0.1:8000`` (or matching
``WHATSAPP_SIDECAR_PORT``). The sidecar binds to loopback by default so it is not exposed
on the Cloud Run service URL (only ``PORT`` /health is).
"""


from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dermascan.shared.load_env import bootstrap_env

from deploy.scripts.run_with_health import run_with_health

logger = logging.getLogger("run_workers_with_turn_sidecar")

_DEFAULT_SIDECAR_HOST: str = "127.0.0.1"
_DEFAULT_SIDECAR_PORT: int = 8000
_SIDECAR_READY_TIMEOUT_S: float = 30.0
_SIDECAR_READY_POLL_S: float = 0.25


def whatsapp_sidecar_enabled() -> bool:
    """
    Decide whether to start the Turn.io sidecar in this container.

    Returns:
        True when the sidecar should run before ``do_work.py``.
    """
    raw = os.getenv("WHATSAPP_SIDECAR_ENABLED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return bool(os.getenv("TURN_API_TOKEN", "").strip())


def _sidecar_host() -> str:
    return os.getenv("WHATSAPP_SIDECAR_HOST", _DEFAULT_SIDECAR_HOST).strip() or _DEFAULT_SIDECAR_HOST


def _sidecar_port() -> int:
    return int(os.getenv("WHATSAPP_SIDECAR_PORT", str(_DEFAULT_SIDECAR_PORT)))


def _wait_sidecar_ready(*, host: str, port: int, timeout_s: float) -> None:
    """
    Poll the sidecar ``/health`` until it responds or timeout.

    Args:
        host: Bind host (loopback in Cloud Run).
        port: Sidecar listen port.
        timeout_s: Maximum seconds to wait.

    Raises:
        RuntimeError: When the sidecar does not become ready in time.
    """
    url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout_s
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except urllib.error.URLError as exc:
            last_error = str(exc.reason or exc)
        time.sleep(_SIDECAR_READY_POLL_S)
    raise RuntimeError(f"Turn sidecar not ready at {url}: {last_error}")


def _start_turn_sidecar() -> subprocess.Popen[bytes] | None:
    """
    Start ``turn.scripts.turn_sidecar_adapter`` via uvicorn when enabled.

    Returns:
        Child process handle, or None when the sidecar is disabled.
    """
    if not whatsapp_sidecar_enabled():
        logger.info("WhatsApp Turn sidecar disabled (set TURN_API_TOKEN or WHATSAPP_SIDECAR_ENABLED=true)")
        return None

    host = _sidecar_host()
    port = _sidecar_port()
    repo_root = os.environ.get("DERMASCAN_ROOT", "/app").rstrip("/")
    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "").strip()
    if existing_path:
        if repo_root not in existing_path.split(os.pathsep):
            env["PYTHONPATH"] = os.pathsep.join([repo_root, existing_path])
    else:
        env["PYTHONPATH"] = repo_root

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "turn.scripts.turn_sidecar_adapter:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        os.getenv("WHATSAPP_SIDECAR_LOG_LEVEL", "info"),
    ]
    logger.info("starting Turn sidecar: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env)
    try:
        _wait_sidecar_ready(host=host, port=port, timeout_s=_SIDECAR_READY_TIMEOUT_S)
    except RuntimeError:
        proc.terminate()
        proc.wait(timeout=5.0)
        raise
    logger.info("Turn sidecar ready on http://%s:%s", host, port)
    return proc


def _stop_sidecar(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def _run_do_work() -> None:
    """Run the worker supervisor and propagate its exit code."""
    sidecar: subprocess.Popen[bytes] | None = None
    try:
        sidecar = _start_turn_sidecar()
        completed = subprocess.run(
            [sys.executable, "dermascan/scripts/do_work.py"],
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)
    finally:
        _stop_sidecar(sidecar)


def main() -> None:
    """
    CLI entrypoint for the workers Cloud Run image.

    Raises:
        SystemExit: On worker or sidecar startup failure.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    bootstrap_env()
    run_with_health(_run_do_work)


if __name__ == "__main__":
    main()

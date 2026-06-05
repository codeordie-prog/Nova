"""
Author: DermaScan Team
Date: 2026-06-04
License: MIT

Modal app that serves Qwen as an OpenAI-compatible HTTP endpoint via vLLM.

A single vLLM OpenAI server is launched once per container on an NVIDIA L40S
GPU and exposed to the internet through ``@modal.web_server``. The orchestrator
consumes this endpoint as its primary chat/tool-calling model (with cloud
providers as fallback) using a standard OpenAI client.

Design notes:
    - vLLM binds ``0.0.0.0`` because ``@modal.web_server`` proxies the external
      network interface, not loopback.
    - The endpoint is currently unauthenticated. To lock it down later, set
      ``requires_proxy_auth=True`` on ``@modal.web_server`` (callers then send
      ``Modal-Key`` / ``Modal-Secret`` headers) or add a vLLM ``--api-key``.
    - Static settings come from ``config/config.json`` (loaded at import time so
      the values are available to the Modal decorators and inside the container).
    - Qwen3 "thinking" mode is a per-request chat-template kwarg controlled by
      the client; it is intentionally NOT a server CLI flag.

Deploy from the project root (where ``pyproject.toml`` lives)::

    modal deploy dermascan/ai_service/modal/qwen_service/app.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import httpx
import modal
from httpx import Response

from dermascan.ai_service.modal.qwen_service.config.loader import QwenConfig, load_config
from dermascan.shared import get_logger
from dermascan.shared.time_it import timeit

# Logger initialization
logger_qwen: logging.Logger = get_logger(__name__)

# Static configuration is resolved at import time. Modal evaluates the class
# decorators (port, concurrency, startup timeout) before any container method
# runs, so these values must exist at module load rather than inside @enter.
_CFG: QwenConfig = load_config()

# Local configuration JSON file path
_LOCAL_CONFIG_JSON: Path = Path(__file__).resolve().parent / "config" / "config.json"

# Loopback URL used only for in-container readiness probing. The public URL is
# assigned by Modal; clients never use this address.
_LOOPBACK_BASE_URL: str = f"http://127.0.0.1:{_CFG.vllm_port}"

# Web-server startup budget. Must exceed the vLLM readiness deadline so Modal
# does not tear the container down while weights are still downloading/loading.
_WEB_STARTUP_TIMEOUT_S: float = _CFG.vllm_startup_timeout_s + 120.0

# Pin Python <3.14 due to vLLM/numba compatibility.
image: modal.Image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "vllm",
    "httpx",
).add_local_python_source("dermascan").add_local_file(
    str(_LOCAL_CONFIG_JSON),
    remote_path="/root/dermascan/ai_service/modal/qwen_service/config/config.json",
)

# Create with: modal secret create huggingface HF_TOKEN=your_token
hf_secret: modal.Secret = modal.Secret.from_name("huggingface")

# Modal app definition
app: modal.App = modal.App("dermascan-qwen")


# Qwen service, served on an L40S GPU as an OpenAI-compatible HTTP endpoint.
# `@modal.concurrent` sits on the class (the methods share one container pool);
# it tells Modal how many simultaneous HTTP requests to route to one replica
# before scaling out, aligned with the vLLM sequence ceiling.
@app.cls(
    gpu="L40S",
    image=image,
    secrets=[hf_secret],
    max_containers=2,
    min_containers=1,
    scaledown_window=600,
)
@modal.concurrent(max_inputs=_CFG.max_concurrent_requests)
class QwenService:
    """
    Modal service that runs a vLLM OpenAI-compatible server for Qwen.

    The vLLM subprocess is started by the ``serve`` web endpoint and reused for
    every request routed to the container. ``@modal.enter`` performs lightweight
    container setup (config + token validation) and ``@modal.exit`` ensures the
    subprocess is terminated cleanly on scaledown.

    Attributes:
        cfg: Loaded static configuration.
        _vllm_proc: Handle to the running vLLM subprocess (``None`` until started).
    """

    @modal.enter()
    def setup(self) -> None:
        """
        Validate container prerequisites before the server is started.

        Raises:
            ValueError: If ``HF_TOKEN`` is missing from the environment.
        """
        self.cfg: QwenConfig = _CFG
        self._vllm_proc: subprocess.Popen[str] | None = None

        token: str = os.environ.get("HF_TOKEN", "").strip()
        if not token:
            raise ValueError("HF_TOKEN environment variable is required")

        logger_qwen.info(
            "Container setup: model_id=%s, served_model_name=%s, port=%s, "
            "tool_call_parser=%s, reasoning_parser=%s, max_concurrent_requests=%s",
            self.cfg.model_id,
            self.cfg.served_model_name,
            self.cfg.vllm_port,
            self.cfg.tool_call_parser,
            self.cfg.reasoning_parser or "<disabled>",
            self.cfg.max_concurrent_requests,
        )

    def _build_command(self) -> list[str]:
        """
        Build the vLLM OpenAI API server command.

        Returns:
            list[str]: Argument vector for the vLLM server process. Binds
            ``0.0.0.0`` so Modal's web-server proxy can reach it.
        """
        cmd: list[str] = [
            "python",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--host",
            "0.0.0.0",
            "--port",
            str(self.cfg.vllm_port),
            "--model",
            self.cfg.model_id,
            "--served-model-name",
            self.cfg.served_model_name,
            "--gpu-memory-utilization",
            str(self.cfg.gpu_memory_utilization),
            "--max-model-len",
            str(self.cfg.max_model_len),
            "--max-num-seqs",
            str(self.cfg.max_num_seqs),
            "--max-num-batched-tokens",
            str(self.cfg.max_num_batched_tokens),
            "--tool-call-parser",
            self.cfg.tool_call_parser,
        ]

        # Auto tool choice requires an explicit flag in addition to the parser.
        if self.cfg.enable_auto_tool_choice:
            cmd.append("--enable-auto-tool-choice")

        # Reasoning extraction is optional; only enable when configured.
        if self.cfg.reasoning_parser:
            cmd.extend(["--reasoning-parser", self.cfg.reasoning_parser])

        return cmd

    @timeit("Qwen vLLM server start")
    def _start_vllm_server(self) -> None:
        """
        Launch the vLLM server subprocess and block until it is ready.

        Raises:
            RuntimeError: If vLLM exits during startup or fails to become ready
                within ``vllm_startup_timeout_s``.
        """
        # Build the vLLM command
        cmd: list[str] = self._build_command()
        logger_qwen.info("Starting vLLM process: %s", " ".join(cmd))

        # vLLM inherits HF_TOKEN from the Modal secret-populated environment.
        self._vllm_proc = subprocess.Popen(
            cmd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        started_at_s: float = time.time()
        last_error: Exception | None = None

        while (time.time() - started_at_s) < self.cfg.vllm_startup_timeout_s:
            # Surface an early crash with captured output instead of waiting out
            # the full timeout.
            if self._vllm_proc.poll() is not None:
                output: str = ""
                try:
                    if self._vllm_proc.stdout is not None:
                        output = self._vllm_proc.stdout.read()[-5000:]
                except Exception:
                    output = ""
                raise RuntimeError(f"vLLM exited during startup. Output:\n{output}")

            try:
                # ``/health`` returns 200 once the engine is ready and does not
                # require auth, so it is the cleanest readiness probe.
                response: Response = httpx.get(f"{_LOOPBACK_BASE_URL}/health", timeout=2.0)
                if response.status_code == 200:
                    logger_qwen.info(
                        "vLLM server ready: served_model_name=%s, port=%s",
                        self.cfg.served_model_name,
                        self.cfg.vllm_port,
                    )
                    return
            except Exception as exc:
                last_error = exc

            time.sleep(1.0)

        raise RuntimeError(f"Timed out waiting for vLLM startup: {last_error}")

    @modal.web_server(port=_CFG.vllm_port, startup_timeout=_WEB_STARTUP_TIMEOUT_S)
    def serve(self) -> None:
        """
        Expose the vLLM OpenAI-compatible API on the container's web port.

        Modal proxies the public HTTPS URL to ``vllm_port`` once this returns and
        the port is accepting connections. The OpenAI routes (``/v1/models``,
        ``/v1/chat/completions``) are served directly by vLLM.
        """
        self._start_vllm_server()

    @modal.exit()
    def stop(self) -> None:
        """
        Terminate the vLLM subprocess on container shutdown.

        Best-effort: escalates to ``kill`` if graceful termination stalls.
        """
        proc: subprocess.Popen[str] | None = getattr(self, "_vllm_proc", None)
        if proc is None or proc.poll() is not None:
            return
        logger_qwen.info("Terminating vLLM process on container exit")
        proc.terminate()
        try:
            proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            logger_qwen.warning("vLLM did not terminate gracefully; killing")
            proc.kill()

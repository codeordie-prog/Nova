"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.



Author: DermaScan Team

Date: 2026-06-04

Licence: PolyForm Noncommercial License 1.0.0



Modal app that serves Qwen as an OpenAI-compatible HTTP endpoint via vLLM.



A single vLLM OpenAI server is launched once per container on an NVIDIA A100-80GB

GPU and exposed to the internet through ``@modal.web_server``.



Design notes:

    - vLLM binds ``0.0.0.0`` because ``@modal.web_server`` proxies the external

      network interface, not loopback.

    - ``serve()`` must return quickly after ``subprocess.Popen``; Modal waits for

      vLLM readiness using ``startup_timeout`` on ``@modal.web_server`` and the

      class-level ``timeout`` / ``startup_timeout`` knobs (defaults are only 300s).

    - Hugging Face weights are cached on a Modal Volume to avoid re-downloading

      ~32B checkpoints on every cold start.

    - Static settings come from ``config/config.json`` (loaded at import time so

      the values are available to the Modal decorators and inside the container).



Deploy from the project root (where ``pyproject.toml`` lives)::



    modal deploy dermascan/ai_service/modal/qwen_service/app.py

"""




from __future__ import annotations



import os

import subprocess

from pathlib import Path



import modal



from dermascan.ai_service.modal.qwen_service.config.loader import QwenConfig, load_config

from dermascan.shared import get_logger



logger_qwen = get_logger(__name__)



_CFG: QwenConfig = load_config()



_LOCAL_CONFIG_JSON: Path = Path(__file__).resolve().parent / "config" / "config.json"



# Modal class + web-server startup budgets for 32B weight load on A100-80GB.

_CLS_TIMEOUT_S: int = int(_CFG.vllm_startup_timeout_s) + 300

_CLS_STARTUP_TIMEOUT_S: int = int(_CFG.vllm_startup_timeout_s) + 120

_WEB_STARTUP_TIMEOUT_S: int = int(_CFG.vllm_startup_timeout_s) + 120



_HF_CACHE_MOUNT: str = "/root/.cache/huggingface"

_VLLM_CACHE_MOUNT: str = "/root/.cache/vllm"



hf_cache_volume: modal.Volume = modal.Volume.from_name(

    "dermascan-qwen-hf-cache",

    create_if_missing=True,

)

vllm_cache_volume: modal.Volume = modal.Volume.from_name(

    "dermascan-qwen-vllm-cache",

    create_if_missing=True,

)



image: modal.Image = modal.Image.debian_slim(python_version="3.12").pip_install(

    "vllm",

    "httpx",

).add_local_python_source("dermascan").add_local_file(

    str(_LOCAL_CONFIG_JSON),

    remote_path="/root/dermascan/ai_service/modal/qwen_service/config/config.json",

)



hf_secret: modal.Secret = modal.Secret.from_name("huggingface")



app: modal.App = modal.App("dermascan-qwen")





@app.cls(

    gpu=_CFG.gpu,

    image=image,

    secrets=[hf_secret],

    volumes={

        _HF_CACHE_MOUNT: hf_cache_volume,

        _VLLM_CACHE_MOUNT: vllm_cache_volume,

    },

    max_containers=2,

    min_containers=1,

    scaledown_window=900,

    timeout=_CLS_TIMEOUT_S,

    startup_timeout=_CLS_STARTUP_TIMEOUT_S,

)

@modal.concurrent(max_inputs=_CFG.max_concurrent_requests)

class QwenService:

    """

    Modal service that runs a vLLM OpenAI-compatible server for Qwen.



    vLLM is spawned as a background subprocess from ``serve`` and kept alive for

    the container lifetime. ``@modal.exit`` terminates it on scaledown.

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

            "Container setup: model_id=%s, served_model_name=%s, gpu=%s, fast_boot=%s, "

            "port=%s, tool_call_parser=%s, reasoning_parser=%s, max_concurrent_requests=%s, "

            "cls_timeout_s=%s, web_startup_timeout_s=%s",

            self.cfg.model_id,

            self.cfg.served_model_name,

            self.cfg.gpu,

            self.cfg.fast_boot,

            self.cfg.vllm_port,

            self.cfg.tool_call_parser,

            self.cfg.reasoning_parser or "<disabled>",

            self.cfg.max_concurrent_requests,

            _CLS_TIMEOUT_S,

            _WEB_STARTUP_TIMEOUT_S,

        )



    def _build_command(self) -> list[str]:

        """

        Build the vLLM OpenAI API server command.



        Returns:

            list[str]: Argument vector for the vLLM server process.

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



        if self.cfg.fast_boot:

            cmd.append("--enforce-eager")



        if self.cfg.enable_auto_tool_choice:

            cmd.append("--enable-auto-tool-choice")



        if self.cfg.reasoning_parser:

            cmd.extend(["--reasoning-parser", self.cfg.reasoning_parser])



        return cmd



    def _launch_vllm_server(self) -> None:

        """

        Spawn vLLM in the background without blocking ``serve``.



        Modal's ``@modal.web_server`` expects this function to return quickly;

        readiness is handled by ``startup_timeout`` while vLLM loads weights.

        """

        if self._vllm_proc is not None and self._vllm_proc.poll() is None:

            logger_qwen.info("vLLM subprocess already running; skipping launch")

            return



        cmd: list[str] = self._build_command()

        logger_qwen.info("Launching vLLM subprocess: %s", " ".join(cmd))



        env = os.environ.copy()

        env.setdefault("HF_HOME", _HF_CACHE_MOUNT)

        env.setdefault("HUGGINGFACE_HUB_CACHE", f"{_HF_CACHE_MOUNT}/hub")

        env.setdefault("TRANSFORMERS_CACHE", _HF_CACHE_MOUNT)

        env.setdefault("VLLM_CACHE_ROOT", _VLLM_CACHE_MOUNT)



        self._vllm_proc = subprocess.Popen(

            cmd,

            env=env,

        )

        logger_qwen.info(

            "vLLM subprocess started pid=%s; Modal will wait up to %ss for readiness",

            self._vllm_proc.pid,

            _WEB_STARTUP_TIMEOUT_S,

        )



    @modal.web_server(port=_CFG.vllm_port, startup_timeout=_WEB_STARTUP_TIMEOUT_S)

    def serve(self) -> None:

        """

        Expose the vLLM OpenAI-compatible API on the container web port.



        Returns immediately after spawning vLLM; Modal routes traffic once the

        port responds within ``startup_timeout``.

        """

        self._launch_vllm_server()



    @modal.exit()

    def stop(self) -> None:

        """Terminate the vLLM subprocess on container shutdown."""

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



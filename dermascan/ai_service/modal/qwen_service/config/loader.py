"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: DermaScan Team
Date: 2026-06-04
Description: Loader for Qwen Modal service static configuration.

Reads non-secret settings from ``config.json``. Secrets (e.g. Hugging Face token)
must come from environment or Modal secrets, not this file.
"""


from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import logging
from typing import Any, Final, TypedDict, cast
from urllib.parse import urlparse

from dermascan.shared import get_logger

CONFIG_PATH: Final[Path] = Path(__file__).resolve().with_name("config.json")
logger_qwen: logging.Logger = get_logger(__name__)


class _QwenConfigDict(TypedDict, total=False):
    """Typed representation of ``config.json``."""

    model_id: str
    served_model_name: str
    gpu: str
    temperature: float
    max_tokens: int
    gpu_memory_utilization: float
    max_model_len: int
    max_num_seqs: int
    max_num_batched_tokens: int
    vllm_host: str
    vllm_port: int
    vllm_base_url: str
    vllm_request_timeout_s: float
    vllm_startup_timeout_s: float
    tool_call_parser: str
    reasoning_parser: str
    enable_auto_tool_choice: bool
    enable_thinking: bool
    fast_boot: bool
    max_concurrent_requests: int


@dataclass(frozen=True)
class QwenConfig:
    """
    Immutable configuration for the Qwen vLLM Modal service.

    Attributes:
        model_id: Hugging Face model identifier.
        served_model_name: Alias exposed by vLLM OpenAI-compatible APIs.
        gpu: Modal GPU type (e.g. ``A100-80GB``, ``H100``, ``L40S``).
        temperature: Default sampling temperature.
        max_tokens: Default max completion tokens.
        gpu_memory_utilization: GPU memory fraction for vLLM.
        max_model_len: Maximum context length.
        max_num_seqs: Maximum concurrent sequences.
        max_num_batched_tokens: Maximum batched tokens per scheduler step.
        vllm_host: vLLM API server host.
        vllm_port: vLLM API server port.
        vllm_base_url: Base URL for vLLM HTTP API (no trailing slash).
        vllm_request_timeout_s: HTTP timeout for completion requests.
        vllm_startup_timeout_s: Max seconds to wait for vLLM readiness.
        tool_call_parser: vLLM ``--tool-call-parser`` value.
        reasoning_parser: Optional vLLM ``--reasoning-parser`` value. Empty
            string disables the flag (no reasoning extraction).
        enable_auto_tool_choice: Whether to pass ``--enable-auto-tool-choice``.
        enable_thinking: Default Qwen3 thinking mode. Applied per request as a
            chat-template kwarg by clients, not as a server CLI flag.
        fast_boot: When True, pass ``--enforce-eager`` to vLLM for faster cold
            starts (skips CUDA graph capture).
        max_concurrent_requests: Max simultaneous HTTP requests routed to one
            replica (``@modal.concurrent(max_inputs=...)``).
    """

    model_id: str
    served_model_name: str
    gpu: str
    temperature: float
    max_tokens: int
    gpu_memory_utilization: float
    max_model_len: int
    max_num_seqs: int
    max_num_batched_tokens: int
    vllm_host: str
    vllm_port: int
    vllm_base_url: str
    vllm_request_timeout_s: float
    vllm_startup_timeout_s: float
    tool_call_parser: str
    reasoning_parser: str
    enable_auto_tool_choice: bool
    enable_thinking: bool
    fast_boot: bool
    max_concurrent_requests: int


DEFAULT_CONFIG: Final[QwenConfig] = QwenConfig(
    model_id="Qwen/Qwen3-32B",
    served_model_name="qwen-3-32b",
    gpu="A100-80GB",
    fast_boot=True,
    temperature=0.0,
    max_tokens=512,
    gpu_memory_utilization=0.92,
    max_model_len=8192,
    max_num_seqs=8,
    max_num_batched_tokens=8192,
    vllm_host="0.0.0.0",
    vllm_port=8000,
    vllm_base_url="http://0.0.0.0:8000",
    vllm_request_timeout_s=180.0,
    vllm_startup_timeout_s=1200.0,
    tool_call_parser="hermes",
    reasoning_parser="",
    enable_auto_tool_choice=True,
    enable_thinking=False,
    max_concurrent_requests=8,
)


def _parse_bool(value: Any) -> bool:
    """
    Parse a boolean value from a string or an integer.
    Args:
        value: The value to parse.
    Returns:
        The boolean value of the value.
    Raises:
        ValueError: If the value is not a boolean or a string.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _build_vllm_base_url(host: str, port: int, base_url: str | None) -> str:
    """
    Build the base URL for the vLLM server.
    Args:
        host: The host of the vLLM server.
        port: The port of the vLLM server.
        base_url: The base URL of the vLLM server.
    Returns:
        The base URL for the vLLM server.
    """
    if base_url:
        return base_url.rstrip("/")
    return f"http://{host}:{port}"


def load_config(path: Path | None = None) -> QwenConfig:
    """
    Load Qwen service configuration from JSON with safe fallback.

    Environment variables may override select fields:
    ``QWEN_MODEL_ID``, ``QWEN_SERVED_MODEL_NAME``, ``QWEN_TOOL_CALL_PARSER``,
    ``VLLM_BASE_URL``, ``VLLM_STARTUP_TIMEOUT_S``.

    Args:
        path: Optional path to config JSON. Defaults to ``config.json`` beside this module.

    Returns:
        QwenConfig: Parsed configuration.

    Raises:
        FileNotFoundError: If the config file is missing and no defaults are usable.
    """
    config_file: Path = path if path is not None else CONFIG_PATH

    if not config_file.exists():
        logger_qwen.warning("Qwen config not found at %s; using defaults", config_file)
        return DEFAULT_CONFIG

    try:
        raw: str = config_file.read_text(encoding="utf-8")
        data: _QwenConfigDict = cast(_QwenConfigDict, json.loads(raw))

        host: str = str(data["vllm_host"])
        port: int = int(data["vllm_port"])
        base_url: str = _build_vllm_base_url(host, port, str(data.get("vllm_base_url", "")))

        cfg = QwenConfig(
            model_id=str(data["model_id"]),
            served_model_name=str(data["served_model_name"]),
            gpu=str(data.get("gpu", DEFAULT_CONFIG.gpu)),
            temperature=float(data["temperature"]),
            max_tokens=int(data["max_tokens"]),
            gpu_memory_utilization=float(data["gpu_memory_utilization"]),
            max_model_len=int(data["max_model_len"]),
            max_num_seqs=int(data["max_num_seqs"]),
            max_num_batched_tokens=int(data["max_num_batched_tokens"]),
            vllm_host=host,
            vllm_port=port,
            vllm_base_url=base_url,
            vllm_request_timeout_s=float(data["vllm_request_timeout_s"]),
            vllm_startup_timeout_s=float(data["vllm_startup_timeout_s"]),
            tool_call_parser=str(data["tool_call_parser"]),
            reasoning_parser=str(data.get("reasoning_parser", "")),
            enable_auto_tool_choice=_parse_bool(data["enable_auto_tool_choice"]),
            enable_thinking=_parse_bool(data["enable_thinking"]),
            fast_boot=_parse_bool(data.get("fast_boot", DEFAULT_CONFIG.fast_boot)),
            max_concurrent_requests=int(
                data.get("max_concurrent_requests", data["max_num_seqs"])
            ),
        )

        logger_qwen.info(
            "Qwen config loaded: model_id=%s, served_model_name=%s, gpu=%s, fast_boot=%s, temperature=%s, max_tokens=%s, gpu_memory_utilization=%s, max_model_len=%s, max_num_seqs=%s, max_num_batched_tokens=%s, vllm_host=%s, vllm_port=%s, vllm_base_url=%s, vllm_request_timeout_s=%s, vllm_startup_timeout_s=%s, tool_call_parser=%s, reasoning_parser=%s, enable_auto_tool_choice=%s, enable_thinking=%s, max_concurrent_requests=%s",
            cfg.model_id,
            cfg.served_model_name,
            cfg.gpu,
            cfg.fast_boot,
            cfg.temperature,
            cfg.max_tokens,
            cfg.gpu_memory_utilization,
            cfg.max_model_len,
            cfg.max_num_seqs,
            cfg.max_num_batched_tokens,
            cfg.vllm_host,
            cfg.vllm_port,
            cfg.vllm_base_url,
            cfg.vllm_request_timeout_s,
            cfg.vllm_startup_timeout_s,
            cfg.tool_call_parser,
            cfg.reasoning_parser,
            cfg.enable_auto_tool_choice,
            cfg.enable_thinking,
            cfg.max_concurrent_requests,
        )
        return cfg
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger_qwen.error("Invalid Qwen config at %s: %s", config_file, exc)
        logger_qwen.warning("Falling back to default Qwen configuration")
        return DEFAULT_CONFIG


if __name__ == "__main__":
    cfg = load_config()
    print(cfg)
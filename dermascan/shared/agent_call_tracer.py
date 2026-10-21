"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-06-07
License: MIT
Description: Per-turn agent model-call tracing.
This module records, for each orchestrator agent turn, how many model calls were issued, which providers were attempted, how many fallbacks occurred, and which tools were invoked. Each completed turn is written as a single structured JSON line to a dedicated trace file so it can be inspected after a run to distinguish legitimate multi-step tool loops from duplicate/looping work.
The tracer is thread-safe: per-turn state is stored in a :class:`ContextVar` (isolated per worker thread), and the file handler uses the stdlib logging lock.
"""


from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final, Optional

_DEFAULT_TRACE_PATH: Final[str] = "logs/agent_model_calls.log"
_ENV_TRACE_PATH: Final[str] = "AGENT_CALL_TRACE_LOG"
_ENV_TRACE_MAX_BYTES: Final[str] = "AGENT_CALL_TRACE_MAX_BYTES"
_ENV_TRACE_BACKUPS: Final[str] = "AGENT_CALL_TRACE_BACKUPS"

_DEFAULT_MAX_BYTES: Final[int] = 10 * 1024 * 1024
_DEFAULT_BACKUPS: Final[int] = 3


@dataclass
class TurnTrace:
    """
    Accumulates model-call telemetry for a single agent turn.

    Attributes:
        request_id: Ingress request identifier for the turn.
        case_id: Case identifier associated with the turn.
        client_id: Client identifier (also the agent thread id).
        channel: Source channel (e.g. ``whatsapp``, ``telegram``).
        flow: Logical flow that triggered the turn (``job`` or ``signal``).
        started_at_s: Monotonic start timestamp in seconds.
        model_calls: Total model handler invocations (attempts) in the turn.
        provider_attempts: Per-provider attempt counts keyed by provider name.
        fallback_count: Number of provider fallbacks that occurred.
        tool_names: Tool names invoked during the turn (in observed order).
        token: ContextVar reset token used to restore prior state.
    """

    request_id: str
    case_id: str
    client_id: str
    channel: str
    flow: str
    started_at_s: float
    model_calls: int = 0
    provider_attempts: dict[str, int] = field(default_factory=dict)
    fallback_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    token: Optional[contextvars.Token] = None


_current_turn: contextvars.ContextVar[Optional[TurnTrace]] = contextvars.ContextVar(
    "agent_current_turn", default=None
)

_tracer_logger: Optional[logging.Logger] = None


def _build_tracer_logger() -> logging.Logger:
    """
    Build (once) the dedicated, non-propagating trace logger.

    Returns:
        logging.Logger: Logger writing JSON lines to the trace file.
    """
    logger = logging.getLogger("dermascan.agent_call_tracer")
    if logger.handlers:
        return logger

    trace_path: Path = Path(os.getenv(_ENV_TRACE_PATH, _DEFAULT_TRACE_PATH))
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes: int = max(1024, int(os.getenv(_ENV_TRACE_MAX_BYTES, str(_DEFAULT_MAX_BYTES))))
    backups: int = max(0, int(os.getenv(_ENV_TRACE_BACKUPS, str(_DEFAULT_BACKUPS))))

    handler = RotatingFileHandler(
        trace_path,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Keep trace lines out of the main app/stderr logs.
    logger.propagate = False
    return logger


def _get_tracer_logger() -> logging.Logger:
    """
    Return the lazily initialized trace logger.

    Returns:
        logging.Logger: The shared trace logger.
    """
    global _tracer_logger
    if _tracer_logger is None:
        _tracer_logger = _build_tracer_logger()
    return _tracer_logger


def start_turn(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    channel: str,
    flow: str,
) -> TurnTrace:
    """
    Begin tracing a new agent turn for the current thread/context.

    Args:
        request_id: Ingress request identifier.
        case_id: Case identifier.
        client_id: Client identifier (agent thread id).
        channel: Source channel.
        flow: Logical flow (``job`` or ``signal``).

    Returns:
        TurnTrace: The active trace record for the turn.
    """
    trace = TurnTrace(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        channel=channel,
        flow=flow,
        started_at_s=time.monotonic(),
    )
    trace.token = _current_turn.set(trace)
    return trace


def record_model_attempt(*, provider: str, success: bool) -> None:
    """
    Record a single model handler attempt for the active turn.

    Args:
        provider: Provider name attempted (e.g. ``qwen:qwen-3-8b``).
        success: Whether the attempt returned a response.
    """
    trace = _current_turn.get()
    if trace is None:
        return
    trace.model_calls += 1
    trace.provider_attempts[provider] = trace.provider_attempts.get(provider, 0) + 1
    if not success:
        trace.fallback_count += 1


def set_tool_names(tool_names: list[str]) -> None:
    """
    Attach the list of tool names invoked during the active turn.

    Args:
        tool_names: Ordered tool names extracted from the agent messages.
    """
    trace = _current_turn.get()
    if trace is None:
        return
    trace.tool_names = list(tool_names)


def extract_tool_names(result: Any) -> list[str]:
    """
    Extract tool names from an agent invocation result.

    Args:
        result: Result returned by ``agent.invoke`` (expected dict with messages).

    Returns:
        list[str]: Tool names referenced by AI tool calls / tool messages.
    """
    names: list[str] = []
    if not isinstance(result, dict):
        return names
    messages = result.get("messages") or []
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            for call in tool_calls:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
                if name:
                    names.append(str(name))
            continue
        message_name = getattr(message, "name", None)
        if message_name and getattr(message, "type", "") == "tool":
            names.append(str(message_name))
    return names


def end_turn(*, status: str, error: Optional[str] = None) -> None:
    """
    Finalize the active turn and write a structured trace line.

    Args:
        status: Outcome label (e.g. ``ok``, ``failed``).
        error: Optional error detail when the turn failed.
    """
    trace = _current_turn.get()
    if trace is None:
        return

    duration_ms: int = int((time.monotonic() - trace.started_at_s) * 1000)
    payload: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "flow": trace.flow,
        "status": status,
        "request_id": trace.request_id,
        "case_id": trace.case_id,
        "client_id": trace.client_id,
        "channel": trace.channel,
        "model_calls": trace.model_calls,
        "provider_attempts": trace.provider_attempts,
        "fallback_count": trace.fallback_count,
        "tool_calls": len(trace.tool_names),
        "tool_names": trace.tool_names,
        "duration_ms": duration_ms,
    }
    if error:
        payload["error"] = error[:500]

    try:
        _get_tracer_logger().info(json.dumps(payload, default=str))
    finally:
        if trace.token is not None:
            _current_turn.reset(trace.token)
        else:
            _current_turn.set(None)


__all__ = [
    "TurnTrace",
    "end_turn",
    "extract_tool_names",
    "record_model_attempt",
    "set_tool_names",
    "start_turn",
]

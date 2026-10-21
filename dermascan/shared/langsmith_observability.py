"""
Author: Dermascan Team
Date: 6-24-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

LangSmith tracing helpers for LangChain agent workers.

Centralizes per-worker project names and RunnableConfig metadata so orchestrator,
knowledge, signal-rules, and report agents produce searchable traces in LangSmith."""


from __future__ import annotations

import logging
import os
from typing import Any, Final, Optional

from langchain_core.runnables import RunnableConfig

_LOGGER: Final[logging.Logger] = logging.getLogger("dermascan.langsmith")

_WORKER_PROJECTS: Final[dict[str, str]] = {
    "orchestrator": "dermascan-orchestrator",
    "knowledge": "dermascan-knowledge",
    "outbound": "dermascan-outbound",
    "reports": "dermascan-reports",
    "signal_rules": "dermascan-signal-rules",
    "pipeline": "dermascan-pipeline",
    "medgemma": "dermascan-medgemma",
}

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def is_tracing_enabled() -> bool:
    """
    Return whether LangSmith tracing is enabled via environment.

    Returns:
        bool: True when ``LANGSMITH_TRACING`` is set to a truthy value.
    """
    return os.getenv("LANGSMITH_TRACING", "").strip().lower() in _TRUTHY


def configure_worker_project(worker: str) -> str:
    """
    Set ``LANGSMITH_PROJECT`` for a worker when not already configured.

    Args:
        worker: Logical worker key (e.g. ``orchestrator``, ``knowledge``).

    Returns:
        str: Project name used for this process.
    """
    default = _WORKER_PROJECTS.get(worker, os.getenv("LANGSMITH_PROJECT", "dermascan-dev"))
    os.environ.setdefault("LANGSMITH_PROJECT", default)
    return os.environ["LANGSMITH_PROJECT"]


def log_tracing_status(worker: str) -> None:
    """
    Log LangSmith tracing readiness for a worker process.

    Args:
        worker: Logical worker key passed to :func:`configure_worker_project`.
    """
    if not is_tracing_enabled():
        return
    project = configure_worker_project(worker)
    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        _LOGGER.warning(
            "LANGSMITH_TRACING is enabled but LANGSMITH_API_KEY is unset; traces will not be sent "
            "(worker=%s project=%s)",
            worker,
            project,
        )
        return
    _LOGGER.info("LangSmith tracing enabled: worker=%s project=%s", worker, project)


def build_invoke_config(
    *,
    thread_id: str,
    checkpoint_ns: str,
    run_name: str,
    channel: str = "",
    flow: str = "",
    client_id: str = "",
    request_id: str = "",
    case_id: str = "",
    message_id: str = "",
    recursion_limit: Optional[int] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    extra_tags: Optional[list[str]] = None,
) -> RunnableConfig:
    """
    Build a LangChain ``RunnableConfig`` with LangSmith metadata and tags.

    Args:
        thread_id: Checkpointer thread id (typically ``client_id``).
        checkpoint_ns: Checkpointer namespace for concurrent flows on one client.
        run_name: Human-readable run label shown in LangSmith.
        channel: Ingress channel (e.g. ``whatsapp``).
        flow: Logical flow label (e.g. ``job``, ``signal``).
        client_id: Client identifier.
        request_id: Ingress request identifier.
        case_id: Case identifier.
        message_id: Source message identifier.
        recursion_limit: Optional agent recursion cap for this invoke.
        extra_metadata: Additional metadata fields to attach to the trace.
        extra_tags: Additional tags beyond ``channel`` and ``flow``.

    Returns:
        RunnableConfig: Config dict for ``agent.invoke``.
    """
    metadata: dict[str, Any] = {
        "client_id": client_id or thread_id,
        "request_id": request_id,
        "case_id": case_id,
        "channel": channel,
        "flow": flow,
    }
    if message_id:
        metadata["message_id"] = message_id
    if extra_metadata:
        metadata.update(extra_metadata)

    tags: list[str] = []
    if channel:
        tags.append(channel)
    if flow:
        tags.append(flow)
    if extra_tags:
        tags.extend(extra_tags)

    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        },
        "run_name": run_name,
        "metadata": metadata,
    }
    if tags:
        config["tags"] = tags
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    return config


__all__ = [
    "build_invoke_config",
    "configure_worker_project",
    "is_tracing_enabled",
    "log_tracing_status",
]

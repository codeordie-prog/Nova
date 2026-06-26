"""
Author: Dermascan Team
Date: 6-26-2026
Licence: MIT

A pipeline tracing for Redis stream produce/consume hops.

Records each stream boundary as a LangSmith ``chain`` run in the pipeline project
so ingress → orchestrator → MedGemma → signals → outbound appears as linked spans
filterable by ``request_id``.  
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import AbstractContextManager, contextmanager
from types import TracebackType
from typing import Any, Callable, Final, Generator, Mapping, Optional, TypeVar

from dermascan.shared.langsmith_observability import is_tracing_enabled

_LOGGER: Final[logging.Logger] = logging.getLogger("dermascan.stream_pipeline")

_ENV_PIPELINE_ENABLED: Final[str] = "LANGSMITH_PIPELINE_TRACING"
_ENV_PIPELINE_PROJECT: Final[str] = "LANGSMITH_PIPELINE_PROJECT"
_DEFAULT_PIPELINE_PROJECT: Final[str] = "dermascan-pipeline"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

_CORRELATION_KEYS: Final[tuple[str, ...]] = (
    "request_id",
    "case_id",
    "client_id",
    "message_id",
    "channel",
    "job_id",
    "medgemma_job_id",
    "signal_type",
    "signal_id",
    "kind",
    "trace_run_id",
    "group_id",
)

T = TypeVar("T")

_LANGSMITH_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_pipeline_tracing_enabled() -> bool:
    """
    Return whether LangSmith pipeline/stream spans should be emitted.

    Returns:
        bool: True when global LangSmith tracing is on and pipeline tracing is not disabled.
    """
    if not is_tracing_enabled():
        return False
    flag = os.getenv(_ENV_PIPELINE_ENABLED, "true").strip().lower()
    return flag in _TRUTHY


def get_pipeline_project() -> str:
    """
    Return the LangSmith project name used for pipeline spans.

    Returns:
        str: Project name (default ``dermascan-pipeline``).
    """
    return os.getenv(_ENV_PIPELINE_PROJECT, _DEFAULT_PIPELINE_PROJECT).strip() or _DEFAULT_PIPELINE_PROJECT


def capture_trace_run_id() -> Optional[str]:
    """
    Capture the active LangSmith run id for parent linking on async hops.

    Returns:
        Optional[str]: Current run tree id when inside an active trace, else None.
    """
    if not is_pipeline_tracing_enabled():
        return None
    try:
        from langsmith.run_helpers import get_current_run_tree

        tree = get_current_run_tree()
        if tree is None:
            return None
        run_id = getattr(tree, "id", None)
        return _valid_langsmith_run_id(str(run_id or ""))
    except Exception:
        return None


def enrich_stream_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    """
    Copy stream fields and attach ``trace_run_id`` when inside an active trace.

    Args:
        fields: Redis stream field mapping to enqueue.

    Returns:
        dict[str, str]: String fields safe for ``XADD``.
    """
    out: dict[str, str] = {
        str(k): str(v) for k, v in fields.items() if v is not None and str(v) != ""
    }
    trace_run_id = capture_trace_run_id()
    if trace_run_id:
        out["trace_run_id"] = trace_run_id
    return out


def _field_value(fields: Mapping[str, Any], key: str) -> str:
    raw = fields.get(key)
    if raw is None:
        raw = fields.get(key.encode()) if isinstance(key, str) else None
    return str(raw or "").strip()


def _valid_langsmith_run_id(value: str) -> Optional[str]:
    """
    Return a LangSmith run id when ``value`` looks like a UUID.

    LangSmith ``parent`` strings are dotted-order paths, not run ids. Passing a
    UUID as ``parent`` raises parse errors; use ``distributed_parent_id`` instead.

    Args:
        value: Candidate parent run id from Redis or context.

    Returns:
        Optional[str]: Normalized UUID string, or None when invalid.
    """
    candidate = value.strip()
    if not candidate or not _LANGSMITH_RUN_ID_RE.fullmatch(candidate):
        return None
    return candidate


def _distributed_parent_from_fields(fields: Optional[Mapping[str, Any]]) -> Optional[str]:
    """
    Extract a valid distributed parent run id from stream message fields.

    Args:
        fields: Redis stream field mapping.

    Returns:
        Optional[str]: Parent run id for distributed tracing, if present.
    """
    return _valid_langsmith_run_id(_field_value(fields or {}, "trace_run_id"))


@contextmanager
def _pipeline_trace_context(
    *,
    distributed_parent_id: Optional[str],
    metadata: Optional[Mapping[str, Any]] = None,
) -> Generator[None, None, None]:
    """
    Optionally set LangSmith distributed parent context for one pipeline span.

    Args:
        distributed_parent_id: Parent run UUID from an upstream trace.
        metadata: Optional metadata merged into the tracing context.
    """
    if not distributed_parent_id:
        yield
        return
    try:
        from langsmith.run_helpers import tracing_context

        with tracing_context(
            distributed_parent_id=distributed_parent_id,
            project_name=get_pipeline_project(),
            metadata=dict(metadata or {}),
            parent=False,
        ):
            yield
    except Exception:
        _LOGGER.exception("failed to set LangSmith distributed parent context")
        yield


def build_correlation_metadata(
    *,
    stream: str,
    event: str,
    direction: str,
    msg_id: str = "",
    fields: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """
    Build LangSmith metadata for a pipeline span.

    Args:
        stream: Redis stream key.
        event: Pipeline event label (``produce``, ``consume``).
        direction: ``produce`` or ``consume``.
        msg_id: Redis stream entry id.
        fields: Message fields when available.
        extra: Additional metadata keys.

    Returns:
        dict[str, Any]: Metadata dict for LangSmith.
    """
    metadata: dict[str, Any] = {
        "stream": stream,
        "event": event,
        "direction": direction,
    }
    if msg_id:
        metadata["msg_id"] = msg_id
    if fields:
        for key in _CORRELATION_KEYS:
            value = _field_value(fields, key)
            if value:
                metadata[key] = value
    if extra:
        metadata.update(extra)
    return metadata


def _emit_pipeline_span(
    *,
    name: str,
    event: str,
    direction: str,
    stream: str,
    msg_id: str = "",
    fields: Optional[Mapping[str, Any]] = None,
    inputs: Optional[dict[str, Any]] = None,
    outputs: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Emit one LangSmith pipeline span (no-op when tracing is disabled).

    Args:
        name: Run name shown in LangSmith.
        event: Pipeline event type.
        direction: ``produce`` or ``consume``.
        stream: Redis stream key.
        msg_id: Stream entry id.
        fields: Correlation fields from the message.
        inputs: Optional LangSmith inputs.
        outputs: Optional LangSmith outputs.
        error: Optional error string.
        duration_ms: Optional handler duration for consume spans.
        extra_metadata: Extra metadata entries.
    """
    if not is_pipeline_tracing_enabled():
        return
    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        return

    metadata = build_correlation_metadata(
        stream=stream,
        event=event,
        direction=direction,
        msg_id=msg_id,
        fields=fields,
        extra=extra_metadata,
    )
    if duration_ms is not None:
        metadata["duration_ms"] = duration_ms

    distributed_parent_id = _distributed_parent_from_fields(fields)

    try:
        from langsmith.run_helpers import trace

        span_inputs = inputs or {
            "stream": stream,
            "msg_id": msg_id,
            "direction": direction,
        }
        with _pipeline_trace_context(
            distributed_parent_id=distributed_parent_id,
            metadata=metadata,
        ):
            with trace(
                name=name,
                run_type="chain",
                inputs=span_inputs,
                project_name=get_pipeline_project(),
                metadata=metadata,
            ) as run:
                if error:
                    run.end(error=error[:500])
                else:
                    run.end(outputs=outputs or {"status": "ok"})
    except Exception:
        _LOGGER.exception("failed to emit LangSmith pipeline span: stream=%s event=%s", stream, event)


def record_stream_produce(
    *,
    stream: str,
    msg_id: str,
    fields: Mapping[str, Any],
    extra_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Record a successful ``XADD`` as a LangSmith pipeline produce span.

    Args:
        stream: Redis stream key written to.
        msg_id: Entry id returned by ``XADD``.
        fields: Fields written to the stream (correlation ids).
        extra_metadata: Optional extra metadata (e.g. ``worker``).
    """
    _emit_pipeline_span(
        name=f"stream:produce:{stream}",
        event="produce",
        direction="produce",
        stream=stream,
        msg_id=msg_id,
        fields=fields,
        inputs={"stream": stream, "msg_id": msg_id},
        outputs={"status": "produced"},
        extra_metadata=extra_metadata,
    )


class trace_stream_consume(AbstractContextManager[None]):
    """
    Context manager that wraps one stream message handler in a LangSmith consume span.

    Args:
        stream: Redis stream key.
        group: Consumer group name.
        consumer: Consumer name within the group.
        msg_id: Redis stream entry id.
        fields: Message fields for correlation metadata.
    """

    def __init__(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        msg_id: str,
        fields: Mapping[str, Any],
    ) -> None:
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._msg_id = msg_id
        self._fields = fields
        self._started_s = time.monotonic()
        self._trace_cm: Any = None
        self._parent_ctx: Any = None
        self._run: Any = None
        self._enabled = is_pipeline_tracing_enabled() and bool(os.getenv("LANGSMITH_API_KEY", "").strip())

    def __enter__(self) -> None:
        if not self._enabled:
            return None
        metadata = build_correlation_metadata(
            stream=self._stream,
            event="consume",
            direction="consume",
            msg_id=self._msg_id,
            fields=self._fields,
            extra={"group": self._group, "consumer": self._consumer},
        )
        distributed_parent_id = _distributed_parent_from_fields(self._fields)
        try:
            from langsmith.run_helpers import trace

            self._parent_ctx = _pipeline_trace_context(
                distributed_parent_id=distributed_parent_id,
                metadata=metadata,
            )
            self._parent_ctx.__enter__()
            self._trace_cm = trace(
                name=f"stream:consume:{self._stream}",
                run_type="chain",
                inputs={
                    "stream": self._stream,
                    "msg_id": self._msg_id,
                    "group": self._group,
                    "consumer": self._consumer,
                },
                project_name=get_pipeline_project(),
                metadata=metadata,
            )
            self._run = self._trace_cm.__enter__()
        except Exception:
            if self._parent_ctx is not None:
                try:
                    self._parent_ctx.__exit__(None, None, None)
                except Exception:
                    pass
                self._parent_ctx = None
            _LOGGER.exception("failed to open LangSmith consume span: stream=%s", self._stream)
            self._trace_cm = None
            self._run = None
        return None

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        if not self._enabled or self._trace_cm is None:
            return False
        duration_ms = int((time.monotonic() - self._started_s) * 1000)
        try:
            if exc is not None:
                self._run.end(
                    error=str(exc)[:500],
                    outputs={"status": "failed", "duration_ms": duration_ms},
                )
            else:
                self._run.end(outputs={"status": "ok", "duration_ms": duration_ms})
        except Exception:
            _LOGGER.exception("failed to close LangSmith consume span: stream=%s", self._stream)
        try:
            self._trace_cm.__exit__(exc_type, exc, tb)
        except Exception:
            _LOGGER.exception("LangSmith trace context exit failed: stream=%s", self._stream)
        if self._parent_ctx is not None:
            try:
                self._parent_ctx.__exit__(exc_type, exc, tb)
            except Exception:
                _LOGGER.exception(
                    "LangSmith distributed parent context exit failed: stream=%s",
                    self._stream,
                )
        return False


def run_traced_stream_handler(
    *,
    stream: str,
    group: str,
    consumer: str,
    msg_id: str,
    fields: Mapping[str, Any],
    handler: Callable[[], T],
) -> T:
    """
    Run a stream message handler inside a LangSmith consume span.

    Args:
        stream: Redis stream key.
        group: Consumer group.
        consumer: Consumer name.
        msg_id: Stream entry id.
        fields: Message fields.
        handler: Callable that processes the message.

    Returns:
        T: Handler return value.
    """
    with trace_stream_consume(
        stream=stream,
        group=group,
        consumer=consumer,
        msg_id=msg_id,
        fields=fields,
    ):
        return handler()


def log_pipeline_tracing_status(worker: str) -> None:
    """
    Log pipeline tracing readiness for a worker process.

    Args:
        worker: Logical worker label for log context.
    """
    if not is_pipeline_tracing_enabled():
        return
    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        _LOGGER.warning(
            "LANGSMITH_PIPELINE_TRACING enabled but LANGSMITH_API_KEY is unset (worker=%s)",
            worker,
        )
        return
    _LOGGER.info(
        "LangSmith pipeline tracing enabled: worker=%s project=%s",
        worker,
        get_pipeline_project(),
    )


__all__ = [
    "build_correlation_metadata",
    "capture_trace_run_id",
    "enrich_stream_fields",
    "get_pipeline_project",
    "is_pipeline_tracing_enabled",
    "log_pipeline_tracing_status",
    "record_stream_produce",
    "run_traced_stream_handler",
    "trace_stream_consume",
]

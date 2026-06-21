"""Helpers for rate limiter and queue depth tests."""

from __future__ import annotations

import uuid
from typing import Any

from dermascan.tests.fixtures.stream_helpers import ensure_consumer_group, read_new_messages


def wire_rate_limiter(
    monkeypatch: Any,
    redis_client: Any,
    *,
    rpm: int = 10,
    window_s: int = 60,
    key_prefix: str = "test:rate_limit:global",
) -> Any:
    """
    Point ``global_rate_limiter`` at a test Redis client and fixed-window settings.

    Returns:
        The patched module.
    """
    import dermascan.shared.global_rate_limiter as rate_limiter

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(rate_limiter, "_redis", redis_client)
    monkeypatch.setattr(rate_limiter, "_RPM", rpm)
    monkeypatch.setattr(rate_limiter, "_WINDOW_S", window_s)
    monkeypatch.setattr(rate_limiter, "_KEY_PREFIX", key_prefix)
    return rate_limiter


def wire_queue_depth(
    monkeypatch: Any,
    redis_client: Any,
    *,
    stream: str | None = None,
    group: str = "test-workers",
    soft: int = 5,
    hard: int = 10,
    wait_budget_ms: int = 200,
    wait_step_ms: int = 25,
) -> tuple[Any, str]:
    """
    Point ``queue_depth_manager`` at a test Redis client and threshold settings.

    Returns:
        Tuple of (patched module, stream key used).
    """
    import dermascan.shared.queue_depth_manager as queue_depth

    suffix: str = uuid.uuid4().hex[:8]
    job_stream: str = stream or f"test:queue:jobs:{suffix}"

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(queue_depth, "_redis", redis_client)
    monkeypatch.setattr(queue_depth, "_JOB_STREAM", job_stream)
    monkeypatch.setattr(queue_depth, "_CONSUMER_GROUP", group)
    monkeypatch.setattr(queue_depth, "_MAX_QUEUE_LEN_SOFT", soft)
    monkeypatch.setattr(queue_depth, "_MAX_QUEUE_LEN_HARD", hard)
    monkeypatch.setattr(queue_depth, "_API_WAIT_BUDGET_MS", wait_budget_ms)
    monkeypatch.setattr(queue_depth, "_API_WAIT_STEP_MS", wait_step_ms)
    return queue_depth, job_stream


def seed_pending_depth(
    redis_client: Any,
    *,
    stream: str,
    group: str,
    count: int,
    consumer: str = "seed-consumer",
) -> None:
    """Create ``count`` XPENDING entries via XADD + XREADGROUP without XACK."""
    ensure_consumer_group(redis_client, stream=stream, group=group)
    for index in range(count):
        redis_client.xadd(stream, {"request_id": f"req-{index}", "payload": "{}"})
        read_new_messages(
            redis_client,
            stream=stream,
            group=group,
            consumer=consumer,
            count=1,
            block_ms=200,
        )

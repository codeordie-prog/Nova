"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Redis Stream helpers for integration tests.

Mirrors production patterns from orchestrator_worker and endpoints:
- ``XGROUP CREATE … id=0 mkstream=True`` (ignore BUSYGROUP)
- ``XADD`` with ``maxlen`` + ``approximate=True``
- ``XREADGROUP`` on ``>`` for new messages
"""


from __future__ import annotations

import uuid
from typing import Any


def unique_stream_bundle(*, prefix: str = "test:orchestrator:jobs") -> dict[str, str]:
    """
    Build unique stream and consumer-group names for test isolation.

    Args:
        prefix: Stream key prefix aligned with production naming.

    Returns:
        Dict with ``stream``, ``group``, and ``dlq`` keys.
    """
    suffix: str = uuid.uuid4().hex[:8]
    return {
        "stream": f"{prefix}:{suffix}",
        "group": f"test-workers:{suffix}",
        "dlq": f"test:orchestrator:dlq:{suffix}",
    }


def ensure_consumer_group(redis_client: Any, *, stream: str, group: str) -> None:
    """
    Create a consumer group if missing (production ``_ensure_consumer_group`` pattern).

    Args:
        redis_client: Connected Redis client.
        stream: Stream key.
        group: Consumer group name.

    Raises:
        Exception: Re-raises unexpected errors (BUSYGROUP is ignored).
    """
    try:
        redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def add_stream_entry(
    redis_client: Any,
    *,
    stream: str,
    request_id: str,
    client_id: str = "client-test",
    case_id: str = "case-test",
    payload: str = "{}",
    maxlen: int = 5000,
    approximate: bool = True,
) -> str:
    """
    Append a job-shaped entry to a stream (production ingress field shape).

    Args:
        redis_client: Connected Redis client.
        stream: Target stream key.
        request_id: Unique request identifier.
        client_id: Client identifier field.
        case_id: Case identifier field.
        payload: JSON payload string.
        maxlen: Stream trim ceiling (``WORKERS_STREAM_MAXLEN`` in production).
        approximate: When True, matches production ``XADD … MAXLEN ~`` lazy trimming.

    Returns:
        str: Redis stream entry ID.
    """
    entry_id: str = redis_client.xadd(
        stream,
        {
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": f"msg-{request_id}",
            "channel": "test",
            "username": "tester",
            "payload": payload,
        },
        maxlen=maxlen,
        approximate=approximate,
    )
    return str(entry_id)


def read_new_messages(
    redis_client: Any,
    *,
    stream: str,
    group: str,
    consumer: str,
    count: int = 1,
    block_ms: int = 100,
) -> list[tuple[str, dict[str, str]]]:
    """
    Read new messages via ``XREADGROUP`` on ``>`` (orchestrator worker pattern).

    Args:
        redis_client: Connected Redis client.
        stream: Stream key.
        group: Consumer group name.
        consumer: Consumer name within the group.
        count: Max messages to read.
        block_ms: Block timeout in milliseconds.

    Returns:
        list[tuple[str, dict[str, str]]]: ``(entry_id, fields)`` pairs.
    """
    raw = redis_client.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={stream: ">"},
        count=count,
        block=block_ms,
    )
    if not raw:
        return []
    return [(str(msg_id), dict(fields)) for _, batch in raw for msg_id, fields in batch]


def flatten_pending(redis_client: Any, *, stream: str, group: str) -> list[tuple[str, str, int]]:
    """
    Return pending entries as ``(entry_id, consumer, idle_ms)`` tuples.

    Args:
        redis_client: Connected Redis client.
        stream: Stream key.
        group: Consumer group name.

    Returns:
        list[tuple[str, str, int]]: Pending entry summaries.
    """
    summary = redis_client.xpending(stream, group)
    if not summary or summary.get("pending", 0) == 0:
        return []

    details = redis_client.xpending_range(stream, group, min="-", max="+", count=100)
    return [(str(row["message_id"]), str(row["consumer"]), int(row["time_since_delivered"])) for row in details]

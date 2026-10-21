"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Helpers for reclaim and idempotency integration tests."""


from __future__ import annotations

from typing import Any

from dermascan.tests.fixtures.stream_helpers import (
    add_stream_entry,
    ensure_consumer_group,
    flatten_pending,
    read_new_messages,
    unique_stream_bundle,
)


def leave_message_pending(
    redis_client: Any,
    *,
    stream: str,
    group: str,
    consumer: str,
    request_id: str,
) -> str:
    """
    Enqueue a job and deliver it to ``consumer`` without XACK (simulates crash).

    Args:
        redis_client: Connected Redis client.
        stream: Stream key.
        group: Consumer group name.
        consumer: Consumer that reads but does not ack.
        request_id: Job request identifier.

    Returns:
        str: Stream entry ID now sitting in XPENDING.
    """
    ensure_consumer_group(redis_client, stream=stream, group=group)
    entry_id = add_stream_entry(redis_client, stream=stream, request_id=request_id)
    batch = read_new_messages(
        redis_client,
        stream=stream,
        group=group,
        consumer=consumer,
        count=1,
        block_ms=500,
    )
    assert len(batch) == 1
    assert batch[0][0] == entry_id
    pending = flatten_pending(redis_client, stream=stream, group=group)
    assert any(msg_id == entry_id for msg_id, _, _ in pending)
    return entry_id


def new_reclaim_stream_bundle() -> dict[str, str]:
    """Unique stream/group/dlq keys for an isolated reclaim test."""
    return unique_stream_bundle(prefix="test:reclaim:jobs")

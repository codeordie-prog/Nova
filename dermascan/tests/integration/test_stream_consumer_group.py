"""Integration tests: Redis Streams consumer group lifecycle.

Concerns: R11 — stream trimming doesn't drop in-flight; C4 — single delivery per group;
          S6 — horizontal worker scaling; L7 — MAXLEN trim behavior.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from dermascan.tests.fixtures.stream_helpers import (
    add_stream_entry,
    ensure_consumer_group,
    flatten_pending,
    read_new_messages,
)


@pytest.mark.integration
class TestStreamConsumerGroup:
    """XADD / XREADGROUP / XACK lifecycle and MAXLEN approximate trim."""

    def test_xreadgroup_delivers_to_one_consumer(
        self,
        real_redis,
        stream_bundle: dict[str, str],
    ) -> None:
        """C4: Two consumers, one message → exactly one consumer receives it."""
        stream: str = stream_bundle["stream"]
        group: str = stream_bundle["group"]
        ensure_consumer_group(real_redis, stream=stream, group=group)

        entry_id = add_stream_entry(real_redis, stream=stream, request_id="req-1")

        first_batch = read_new_messages(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            count=1,
            block_ms=200,
        )
        second_batch = read_new_messages(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-b",
            count=1,
            block_ms=100,
        )

        assert len(first_batch) == 1
        assert first_batch[0][0] == entry_id
        assert first_batch[0][1]["request_id"] == "req-1"
        assert second_batch == []

    def test_xack_removes_from_pending(
        self,
        real_redis,
        stream_bundle: dict[str, str],
    ) -> None:
        """Acknowledged message no longer appears in XPENDING."""
        stream: str = stream_bundle["stream"]
        group: str = stream_bundle["group"]
        ensure_consumer_group(real_redis, stream=stream, group=group)

        entry_id = add_stream_entry(real_redis, stream=stream, request_id="req-ack")

        read_new_messages(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            count=1,
            block_ms=200,
        )
        assert len(flatten_pending(real_redis, stream=stream, group=group)) == 1

        real_redis.xack(stream, group, entry_id)

        assert flatten_pending(real_redis, stream=stream, group=group) == []

    def test_maxlen_trim_retains_pending(
        self,
        real_redis,
        stream_bundle: dict[str, str],
    ) -> None:
        """R11/L7: Flood beyond MAXLEN → pending/unacked not silently lost."""
        stream: str = stream_bundle["stream"]
        group: str = stream_bundle["group"]
        trim_maxlen: int = 5
        ensure_consumer_group(real_redis, stream=stream, group=group)

        held_id = add_stream_entry(
            real_redis,
            stream=stream,
            request_id="req-held",
            maxlen=trim_maxlen,
        )
        read_new_messages(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-held",
            count=1,
            block_ms=200,
        )

        for index in range(20):
            add_stream_entry(
                real_redis,
                stream=stream,
                request_id=f"req-flood-{index}",
                maxlen=trim_maxlen,
            )

        pending = flatten_pending(real_redis, stream=stream, group=group)
        pending_ids = [entry_id for entry_id, _, _ in pending]
        assert held_id in pending_ids

        # Production acks then deletes; pending entry must remain actionable.
        assert real_redis.xack(stream, group, held_id) == 1

    def test_multiple_workers_partition_messages(
        self,
        real_redis,
        stream_bundle: dict[str, str],
    ) -> None:
        """S6: Four consumers on one group drain N messages with no duplicates."""
        stream: str = stream_bundle["stream"]
        group: str = stream_bundle["group"]
        message_count: int = 8
        worker_count: int = 4
        ensure_consumer_group(real_redis, stream=stream, group=group)

        for index in range(message_count):
            add_stream_entry(real_redis, stream=stream, request_id=f"req-{index}")

        collected_ids: list[str] = []
        lock = threading.Lock()

        def drain_worker(worker_name: str) -> None:
            while True:
                batch = read_new_messages(
                    real_redis,
                    stream=stream,
                    group=group,
                    consumer=worker_name,
                    count=2,
                    block_ms=200,
                )
                if not batch:
                    return
                with lock:
                    collected_ids.extend(msg_id for msg_id, _ in batch)
                for msg_id, _ in batch:
                    real_redis.xack(stream, group, msg_id)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(drain_worker, f"worker-{index}")
                for index in range(worker_count)
            ]
            for future in as_completed(futures):
                future.result(timeout=10)

        assert len(collected_ids) == message_count
        assert len(set(collected_ids)) == message_count
        assert flatten_pending(real_redis, stream=stream, group=group) == []

    @pytest.mark.slow
    def test_stream_retention_window_documented(
        self,
        real_redis,
        stream_bundle: dict[str, str],
    ) -> None:
        """L7: MAXLEN caps stream size; production uses lazy approximate trimming."""
        stream: str = stream_bundle["stream"]
        group: str = stream_bundle["group"]
        trim_maxlen: int = 10
        flood_count: int = 50
        ensure_consumer_group(real_redis, stream=stream, group=group)

        # Exact trim (reference): hard cap enforced on every XADD.
        exact_stream: str = f"{stream}:exact"
        for index in range(flood_count):
            real_redis.xadd(
                exact_stream,
                {"request_id": f"req-exact-{index}"},
                maxlen=trim_maxlen,
                approximate=False,
            )
        exact_length: int = int(real_redis.xlen(exact_stream))
        assert exact_length <= trim_maxlen, (
            f"exact MAXLEN should cap at {trim_maxlen}, observed {exact_length}"
        )

        # Production-style approximate trim: needs sustained pressure before eviction.
        heavy_flood_count: int = 500
        for index in range(heavy_flood_count):
            add_stream_entry(
                real_redis,
                stream=stream,
                request_id=f"req-retention-{index}",
                payload="x" * 200,
                maxlen=trim_maxlen,
                approximate=True,
            )

        approx_length: int = int(real_redis.xlen(stream))
        assert approx_length < heavy_flood_count, (
            "approximate MAXLEN should trim under sustained flood"
        )
        assert approx_length <= trim_maxlen * 2, (
            f"documented approximate bound: maxlen={trim_maxlen}, "
            f"observed_xlen={approx_length}, flood_count={heavy_flood_count}. "
            "Production uses approximate=True (~) for CPU efficiency; "
            "observed length may exceed maxlen until whole nodes are evicted."
        )

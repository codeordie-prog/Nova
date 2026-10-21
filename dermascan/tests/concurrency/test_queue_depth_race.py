"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Concurrency tests: queue depth reads under concurrent enqueue/ack.

Concerns: stable XPENDING depth under concurrent producers/consumers.
Requires real Redis (testcontainers).
"""


from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from dermascan.tests.fixtures.admission_helpers import seed_pending_depth, wire_queue_depth
from dermascan.tests.fixtures.stream_helpers import add_stream_entry, ensure_consumer_group, read_new_messages


@pytest.mark.concurrency
class TestQueueDepthRace:
    """Depth readings remain consistent under concurrent stream activity."""

    def test_depth_stable_under_concurrent_enqueue_ack(
        self,
        real_redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrent XADD + XACK → depth never negative or inconsistent."""
        queue_depth, stream = wire_queue_depth(
            monkeypatch,
            real_redis,
            soft=50,
            hard=100,
        )
        group = "test-workers"
        ensure_consumer_group(real_redis, stream=stream, group=group)
        lock = threading.Lock()
        observed_depths: list[int] = []

        def worker(worker_id: int) -> None:
            for index in range(5):
                entry_id = add_stream_entry(
                    real_redis,
                    stream=stream,
                    request_id=f"{worker_id}-{index}-{uuid.uuid4().hex[:6]}",
                )
                read_new_messages(
                    real_redis,
                    stream=stream,
                    group=group,
                    consumer=f"worker-{worker_id}",
                    count=1,
                    block_ms=200,
                )
                with lock:
                    depth = queue_depth._get_queue_depth()
                    observed_depths.append(depth)
                    assert depth >= 0
                real_redis.xack(stream, group, entry_id)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(worker, range(8)))

        assert queue_depth._get_queue_depth() == 0
        assert all(depth >= 0 for depth in observed_depths)

    def test_pressure_level_transitions_are_monotonic(
        self,
        real_redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """green → yellow → red transitions align with actual XPENDING counts."""
        states: list[str] = []

        for count, expected_state in ((2, "green"), (4, "yellow"), (6, "red")):
            queue_depth, stream = wire_queue_depth(
                monkeypatch,
                real_redis,
                soft=3,
                hard=6,
            )
            seed_pending_depth(
                real_redis,
                stream=stream,
                group="test-workers",
                count=count,
                consumer=f"seed-{count}",
            )
            state, depth = queue_depth._check_queue_pressure()
            states.append(state)
            assert depth == count
            assert state == expected_state

        assert states == ["green", "yellow", "red"]

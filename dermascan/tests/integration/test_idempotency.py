"""Integration tests: duplicate message idempotency.

Concerns: R1 — exactly-once-ish processing; C7 — idempotency under burst.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from dermascan.backend.services.agents import reclaim_logic
from dermascan.tests.fixtures.reclaim_helpers import leave_message_pending, new_reclaim_stream_bundle
from dermascan.tests.fixtures.stream_helpers import flatten_pending


@pytest.mark.integration
class TestIdempotency:
    """Duplicate delivery processed once across reclaim and concurrent markers."""

    def test_mark_processed_set_nx_wins_once(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """C7: Parallel mark_processed on same msg → only one SET NX wins."""
        stream = "test:idempotency:stream"
        msg_id = "1700000000000-0"
        thread_count: int = 20

        def try_mark() -> bool:
            return reclaim_logic.mark_processed(stream, msg_id, ttl_sec=60)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            results = list(executor.map(lambda _: try_mark(), range(thread_count)))

        assert sum(1 for won in results if won) == 1
        assert sum(1 for won in results if not won) == thread_count - 1
        assert reclaim_logic.is_processed(stream, msg_id) is True

    def test_second_pass_acks_without_reprocessing(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R1: Already-processed pending entry is acked without invoking handler."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        entry_id = leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            request_id="req-second-pass",
        )

        handler_calls: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            handler_calls.append(msg_id)

        # Simulate crash after mark_processed but before XACK.
        assert reclaim_logic.mark_processed(stream, entry_id) is True

        reclaimed = reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", handler, min_idle_ms=0,
        )

        assert reclaimed == 0
        assert handler_calls == []
        assert flatten_pending(real_redis, stream=stream, group=group) == []
        assert reclaim_logic.is_processed(stream, entry_id) is True

    def test_handler_uses_mark_processed_before_side_effects(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R1: Production pattern — mark_processed gates side effects on reclaim."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            request_id="req-gate",
        )

        side_effects: list[str] = []

        def gated_handler(msg_id: str, fields: dict) -> None:
            if reclaim_logic.is_processed(stream, msg_id):
                real_redis.xack(stream, group, msg_id)
                real_redis.xdel(stream, msg_id)
                return
            if not reclaim_logic.mark_processed(stream, msg_id):
                return
            side_effects.append(msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", gated_handler, min_idle_ms=0,
        )
        reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", gated_handler, min_idle_ms=0,
        )

        assert len(side_effects) == 1
        assert flatten_pending(real_redis, stream=stream, group=group) == []

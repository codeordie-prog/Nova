"""Integration tests: crashed-consumer pending message recovery.

Concerns: R2 — crashed worker recovery; R3 — poison message → DLQ.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest

from dermascan.backend.services.agents import reclaim_logic
from dermascan.tests.fixtures.reclaim_helpers import leave_message_pending, new_reclaim_stream_bundle
from dermascan.tests.fixtures.stream_helpers import flatten_pending


@pytest.mark.integration
class TestReclaimRecovery:
    """Pending messages reclaimed after consumer crash (no XACK)."""

    def test_unacked_message_reclaimed_to_new_consumer(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R2: Consumer A receives, never acks → reclaimed and processed by B."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        entry_id = leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            request_id="req-reclaim-1",
        )

        handled: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            handled.append(msg_id)
            reclaim_logic.mark_processed(stream, msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        reclaimed = reclaim_logic.reclaim_stale_pending(
            stream,
            group,
            "worker-b",
            handler,
            min_idle_ms=0,
        )

        assert reclaimed == 1
        assert handled == [entry_id]
        assert flatten_pending(real_redis, stream=stream, group=group) == []

    def test_reclaimed_message_processed_once(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R1/R2: Reclaimed message handled exactly once via idempotency."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            request_id="req-once",
        )

        handler_calls: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            if not reclaim_logic.mark_processed(stream, msg_id):
                return
            handler_calls.append(msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        first_pass = reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", handler, min_idle_ms=0,
        )
        second_pass = reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", handler, min_idle_ms=0,
        )

        assert first_pass == 1
        assert second_pass == 0
        assert len(handler_calls) == 1
        assert flatten_pending(real_redis, stream=stream, group=group) == []

    def test_poison_message_moves_to_dlq_after_max_attempts(
        self,
        real_redis,
        reclaim_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R3: Handler keeps failing → message lands in DLQ after max reclaim attempts."""
        monkeypatch.setattr(reclaim_logic, "MAX_RECLAIM_ATTEMPTS", 2)

        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        dlq: str = bundle["dlq"]
        entry_id = leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-a",
            request_id="req-poison",
        )

        def always_fails(_msg_id: str, _fields: dict) -> None:
            raise RuntimeError("simulated handler failure")

        reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", always_fails, dlq_stream=dlq, min_idle_ms=0,
        )
        reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", always_fails, dlq_stream=dlq, min_idle_ms=0,
        )
        reclaim_logic.reclaim_stale_pending(
            stream, group, "worker-b", always_fails, dlq_stream=dlq, min_idle_ms=0,
        )

        dlq_len: int = int(real_redis.xlen(dlq))
        assert dlq_len == 1
        dlq_entries = real_redis.xrange(dlq, count=1)
        assert len(dlq_entries) == 1
        _dlq_id, dlq_fields = dlq_entries[0]
        assert dlq_fields.get("original_msg_id") == entry_id
        assert dlq_fields.get("reason") == "max_reclaim_attempts_exceeded"
        assert flatten_pending(real_redis, stream=stream, group=group) == []

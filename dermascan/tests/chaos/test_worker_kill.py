"""Chaos tests: kill consumer mid-job.

Concerns: R2 — crashed worker recovery via reclaim.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest

from dermascan.backend.services.agents import reclaim_logic
from dermascan.tests.fixtures.reclaim_helpers import leave_message_pending, new_reclaim_stream_bundle
from dermascan.tests.fixtures.stream_helpers import flatten_pending


@pytest.mark.chaos
class TestWorkerKill:
    """Consumer dies mid-job → reclaim re-delivers and completes once."""

    def test_kill_consumer_mid_job_reclaims_message(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R2: Consumer killed after XREADGROUP, before XACK → message re-delivered."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        entry_id = leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-killed",
            request_id="req-kill-mid-job",
        )

        reclaimed_ids: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            reclaimed_ids.append(msg_id)
            reclaim_logic.mark_processed(stream, msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        count = reclaim_logic.reclaim_stale_pending(
            stream, group, "recovery-worker", handler, min_idle_ms=0,
        )

        assert count == 1
        assert reclaimed_ids == [entry_id]
        assert flatten_pending(real_redis, stream=stream, group=group) == []

    def test_reclaimed_job_completes_without_double_processing(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """R1/R2: Reclaimed job completes once; idempotency marker prevents dup."""
        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]
        leave_message_pending(
            real_redis,
            stream=stream,
            group=group,
            consumer="worker-killed",
            request_id="req-kill-once",
        )

        side_effects: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            if not reclaim_logic.mark_processed(stream, msg_id):
                return
            side_effects.append(msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        first = reclaim_logic.reclaim_stale_pending(
            stream, group, "recovery-a", handler, min_idle_ms=0,
        )
        second = reclaim_logic.reclaim_stale_pending(
            stream, group, "recovery-b", handler, min_idle_ms=0,
        )

        assert first == 1
        assert second == 0
        assert len(side_effects) == 1
        assert flatten_pending(real_redis, stream=stream, group=group) == []

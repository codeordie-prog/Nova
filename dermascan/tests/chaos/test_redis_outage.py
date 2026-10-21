"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Chaos tests: Redis outage mid-flight.

Concerns: R4 — Redis transient failure; R5 — Redis hard failure.
"""


from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dermascan.shared import redis_stability


@pytest.mark.chaos
class TestRedisOutage:
    """Redis drops mid-flight → retries or graceful failure."""

    def test_transient_redis_blip_retries_and_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R4: Brief ConnectionError → retry succeeds; operation completes."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "3")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.01")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "0.05")
        monkeypatch.setattr(redis_stability.time, "sleep", lambda _: None)

        calls = {"n": 0}

        def flaky_ping() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise RedisConnectionError("simulated outage")
            return "PONG"

        result = redis_stability.run_with_redis_retries(
            flaky_ping,
            operation_name="chaos_ping",
        )

        assert result == "PONG"
        assert calls["n"] == 2

    def test_sustained_redis_outage_surfaces_after_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R5: Redis unavailable → error re-raised after retry budget exhausted."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "2")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.01")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "0.05")
        monkeypatch.setattr(redis_stability.time, "sleep", lambda _: None)

        def always_fails() -> None:
            raise RedisConnectionError("sustained outage")

        with pytest.raises(RedisConnectionError, match="sustained outage"):
            redis_stability.run_with_redis_retries(
                always_fails,
                operation_name="chaos_sustained",
                attempts=2,
            )

    def test_recovery_drains_backlog_after_blip(
        self,
        real_redis,
        reclaim_env: None,
    ) -> None:
        """Recovery: pending jobs reclaimed and cleared after simulated worker crash."""
        from dermascan.backend.services.agents import reclaim_logic
        from dermascan.tests.fixtures.reclaim_helpers import leave_message_pending, new_reclaim_stream_bundle
        from dermascan.tests.fixtures.stream_helpers import flatten_pending

        bundle = new_reclaim_stream_bundle()
        stream: str = bundle["stream"]
        group: str = bundle["group"]

        for index in range(3):
            leave_message_pending(
                real_redis,
                stream=stream,
                group=group,
                consumer=f"worker-crash-{index}",
                request_id=f"req-backlog-{index}",
            )

        assert len(flatten_pending(real_redis, stream=stream, group=group)) == 3

        completed: list[str] = []

        def handler(msg_id: str, fields: dict) -> None:
            completed.append(msg_id)
            reclaim_logic.mark_processed(stream, msg_id)
            real_redis.xack(stream, group, msg_id)
            real_redis.xdel(stream, msg_id)

        reclaimed_total = 0
        for _ in range(3):
            reclaimed_total += reclaim_logic.reclaim_stale_pending(
                stream, group, "recovery-worker", handler, min_idle_ms=0,
            )

        assert reclaimed_total == 3
        assert len(completed) == 3
        assert flatten_pending(real_redis, stream=stream, group=group) == []

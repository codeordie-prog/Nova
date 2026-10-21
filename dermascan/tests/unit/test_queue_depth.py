"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for queue depth manager (green/yellow/red backpressure).

Concerns: S3 — queue backpressure thresholds; S4 — wait-budget behavior.
Source: dermascan/shared/queue_depth_manager.py
"""


from __future__ import annotations

import pytest

from dermascan.tests.fixtures.admission_helpers import seed_pending_depth, wire_queue_depth


@pytest.mark.unit
class TestQueueDepthManager:
    """XPENDING-based pressure levels and wait-budget exhaustion."""

    def test_green_below_soft_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S3: Depth below soft limit → green."""
        queue_depth, stream = wire_queue_depth(monkeypatch, fake_redis, soft=5, hard=10)
        seed_pending_depth(fake_redis, stream=stream, group="test-workers", count=4)

        state, depth = queue_depth._check_queue_pressure()

        assert state == "green"
        assert depth == 4

    def test_yellow_between_soft_and_hard(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S3: Depth in [soft, hard) → yellow."""
        queue_depth, stream = wire_queue_depth(monkeypatch, fake_redis, soft=5, hard=10)
        seed_pending_depth(fake_redis, stream=stream, group="test-workers", count=7)

        state, depth = queue_depth._check_queue_pressure()

        assert state == "yellow"
        assert depth == 7

    def test_red_at_or_above_hard_threshold(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S3/L4: Depth >= hard limit → red."""
        queue_depth, stream = wire_queue_depth(monkeypatch, fake_redis, soft=5, hard=10)
        seed_pending_depth(fake_redis, stream=stream, group="test-workers", count=10)

        state, depth = queue_depth._check_queue_pressure()

        assert state == "red"
        assert depth == 10

    @pytest.mark.asyncio
    async def test_wait_budget_exhaustion_returns_last_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S4: Yellow persists beyond wait budget → last observed pressure returned."""
        queue_depth, stream = wire_queue_depth(
            monkeypatch,
            fake_redis,
            soft=5,
            hard=10,
            wait_budget_ms=50,
            wait_step_ms=25,
        )
        seed_pending_depth(fake_redis, stream=stream, group="test-workers", count=7)

        state, depth = await queue_depth._wait_for_queue_capacity()

        assert state == "yellow"
        assert depth == 7

    @pytest.mark.asyncio
    async def test_green_recovery_within_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S4: Yellow clears to green within wait budget → admit after retry."""
        queue_depth, stream = wire_queue_depth(
            monkeypatch,
            fake_redis,
            soft=5,
            hard=10,
            wait_budget_ms=200,
            wait_step_ms=25,
        )
        seed_pending_depth(fake_redis, stream=stream, group="test-workers", count=7)

        async def drain_pending() -> None:
            import anyio

            await anyio.sleep(0.05)
            pending = fake_redis.xpending_range(stream, "test-workers", min="-", max="+", count=100)
            for row in pending:
                msg_id = row["message_id"]
                fake_redis.xack(stream, "test-workers", msg_id)

        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain_pending)
            state, depth = await queue_depth._wait_for_queue_capacity()

        assert state == "green"
        assert depth == 0

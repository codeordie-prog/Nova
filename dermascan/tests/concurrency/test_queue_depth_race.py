"""Concurrency tests: queue depth reads under concurrent enqueue/ack.

Concerns: stable XPENDING depth under concurrent producers/consumers.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.concurrency
class TestQueueDepthRace:
    """Depth readings remain consistent under concurrent stream activity."""

    def test_depth_stable_under_concurrent_enqueue_ack(self, real_redis) -> None:
        """Concurrent XADD + XACK → depth never negative or inconsistent."""
        pytest.skip("Scaffold: implementation pending")

    def test_pressure_level_transitions_are_monotonic(self, real_redis) -> None:
        """green → yellow → red transitions align with actual XPENDING counts."""
        pytest.skip("Scaffold: implementation pending")

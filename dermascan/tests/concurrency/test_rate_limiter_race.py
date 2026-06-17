"""Concurrency tests: rate limiter atomicity under parallel admission.

Concerns: C3 — rate-limiter atomicity (INCRBY pipeline).
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.concurrency
class TestRateLimiterRace:
    """M threads hammer one window; total allowed must equal RPM exactly."""

    def test_parallel_consume_never_exceeds_rpm(self, real_redis) -> None:
        """C3: M parallel consumes → total allowed == RPM, zero over-admission."""
        pytest.skip("Scaffold: implementation pending")

    def test_no_torn_reads_on_window_boundary(self, real_redis) -> None:
        """C3: Concurrent requests at window rollover → count remains accurate."""
        pytest.skip("Scaffold: implementation pending")

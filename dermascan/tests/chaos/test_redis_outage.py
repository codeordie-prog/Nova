"""Chaos tests: Redis outage mid-flight.

Concerns: R4 — Redis transient failure; R5 — Redis hard failure.
Fault injection via monkeypatch or toxiproxy (optional).
"""

from __future__ import annotations

import pytest


@pytest.mark.chaos
class TestRedisOutage:
    """Redis drops mid-flight → retries or graceful 5xx."""

    def test_transient_redis_blip_retries_and_succeeds(self) -> None:
        """R4: Brief ConnectionError → retry succeeds; client sees success."""
        pytest.skip("Scaffold: implementation pending")

    def test_sustained_redis_outage_returns_graceful_5xx(self, app_client) -> None:
        """R5: Redis unavailable → graceful 5xx, no worker crash."""
        pytest.skip("Scaffold: implementation pending")

    def test_recovery_drains_backlog_after_blip(self, real_redis) -> None:
        """Recovery SLO: backlog drains to zero within N seconds after blip."""
        pytest.skip("Scaffold: implementation pending")

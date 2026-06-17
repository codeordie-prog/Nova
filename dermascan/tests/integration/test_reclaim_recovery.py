"""Integration tests: crashed-consumer pending message recovery.

Concerns: R2 — crashed worker recovery via reclaim_stale_pending.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestReclaimRecovery:
    """Pending messages reclaimed after consumer crash (no XACK)."""

    def test_unacked_message_reclaimed_to_new_consumer(self, real_redis) -> None:
        """R2: Consumer A receives, never acks → reclaimed and processed by B."""
        pytest.skip("Scaffold: implementation pending")

    def test_reclaimed_message_processed_once(self, real_redis) -> None:
        """R1/R2: Reclaimed message handled exactly once via idempotency."""
        pytest.skip("Scaffold: implementation pending")

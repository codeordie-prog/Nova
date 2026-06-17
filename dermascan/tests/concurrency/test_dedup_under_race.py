"""Concurrency tests: media-group batching and idempotency under burst.

Concerns: C6 — media-group batching race; C7 — idempotency under burst.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.concurrency
class TestDedupUnderRace:
    """Album burst and parallel reclaim must not duplicate enqueue."""

    def test_media_group_single_batch_claimed(self, real_redis) -> None:
        """C6: Burst album members → one batch claimed, quiet-window respected."""
        pytest.skip("Scaffold: implementation pending")

    def test_no_duplicate_enqueue_under_burst(self, real_redis) -> None:
        """C6/C7: Parallel album + reclaim → single job enqueued per logical message."""
        pytest.skip("Scaffold: implementation pending")

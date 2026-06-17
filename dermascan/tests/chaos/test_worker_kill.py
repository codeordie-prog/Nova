"""Chaos tests: kill consumer mid-job.

Concerns: R2 — crashed worker recovery via reclaim.
Requires real Redis + running worker process (or simulated consumer).
"""

from __future__ import annotations

import pytest


@pytest.mark.chaos
@pytest.mark.slow
class TestWorkerKill:
    """Kill consumer mid-job → reclaim re-delivers and completes."""

    def test_kill_consumer_mid_job_reclaims_message(self, real_redis) -> None:
        """R2: Consumer killed after XREADGROUP, before XACK → message re-delivered."""
        pytest.skip("Scaffold: implementation pending")

    def test_reclaimed_job_completes_without_double_processing(self, real_redis) -> None:
        """R1/R2: Reclaimed job completes once; idempotency marker prevents dup."""
        pytest.skip("Scaffold: implementation pending")

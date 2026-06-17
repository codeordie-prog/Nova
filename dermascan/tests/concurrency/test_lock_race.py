"""Concurrency tests: per-client lock race correctness.

Concerns: C1 — per-client serialization; C2 — different clients run in parallel.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.concurrency
class TestLockRace:
    """N threads contend on client_lock; overlap must never exceed 1."""

    def test_client_lock_serializes_same_client_id(self, real_redis) -> None:
        """C1: 20 threads same client_id → max_overlap == 1."""
        pytest.skip("Scaffold: implementation pending")

    def test_different_client_ids_run_in_parallel(self, real_redis) -> None:
        """C2: Distinct client_ids → no false serialization (overlap > 1 allowed)."""
        pytest.skip("Scaffold: implementation pending")

"""Integration tests: per-client lock serialization under real Redis.

Concerns: C1 — per-client serialization.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestLockSerialization:
    """Concurrent holders on same client_id serialize correctly."""

    def test_same_client_id_serializes_critical_sections(self, real_redis) -> None:
        """C1: N concurrent tasks same client_id → critical sections never overlap."""
        pytest.skip("Scaffold: implementation pending")

    def test_checkpoint_not_corrupted_under_serialization(self, real_redis) -> None:
        """C1: Serialized updates preserve checkpoint integrity."""
        pytest.skip("Scaffold: implementation pending")

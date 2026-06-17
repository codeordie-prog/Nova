"""Unit tests for per-client distributed lock (NX/PX acquire, compare-and-delete release).

Concerns: R6 — lock safety on TTL expiry; C1/C2 — per-client serialization;
          L9 — lock wait-budget timeout.
Source: dermascan/backend/services/agents/locks.py
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestClientLock:
    """Lua-backed acquire, release, and TTL semantics."""

    def test_acquire_sets_nx_px_key(self, fake_redis) -> None:
        """Holder acquires lock with NX+PX; key exists with correct TTL."""
        pytest.skip("Scaffold: implementation pending")

    def test_release_compare_and_delete(self, fake_redis) -> None:
        """Only the lock holder can delete the key (compare-and-delete Lua)."""
        pytest.skip("Scaffold: implementation pending")

    def test_ttl_expiry_allows_reacquire(self, fake_redis) -> None:
        """R6: After TTL expires, a new holder can acquire."""
        pytest.skip("Scaffold: implementation pending")

    def test_stale_holder_release_is_noop(self, fake_redis) -> None:
        """R6: Holder A's TTL expires, B acquires; A's release is a no-op."""
        pytest.skip("Scaffold: implementation pending")

    def test_wait_budget_raises_lock_acquisition_error(self, fake_redis) -> None:
        """L9: Lock held beyond CLIENT_LOCK_WAIT_MS → LockAcquisitionError."""
        pytest.skip("Scaffold: implementation pending")

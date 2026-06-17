"""Unit tests for global rate limiter (fixed-window RPM admission).

Concerns: S2 — rate-limit window accuracy.
Source: dermascan/shared/global_rate_limiter.py
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestGlobalRateLimiter:
    """Fixed-window counting, window rollover, and RPM=0 bypass."""

    def test_allows_up_to_rpm(self, monkeypatch: pytest.MonkeyPatch, fake_redis) -> None:
        """S2: Exactly RPM requests allowed per window; (RPM+1) rejected with retry_after >= 1."""
        pytest.skip("Scaffold: implementation pending")

    def test_window_rollover_resets(self, monkeypatch: pytest.MonkeyPatch, fake_redis) -> None:
        """S2: After window expires, counter resets and requests are allowed again (freezegun)."""
        pytest.skip("Scaffold: implementation pending")

    def test_rpm_zero_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """S2: When _RPM <= 0, _enforce_global_rate_limit returns without touching Redis."""
        pytest.skip("Scaffold: implementation pending")

    def test_rejected_returns_retry_after(self, monkeypatch: pytest.MonkeyPatch, fake_redis) -> None:
        """S1/S2: Over-RPM rejection includes Retry-After header semantics."""
        pytest.skip("Scaffold: implementation pending")

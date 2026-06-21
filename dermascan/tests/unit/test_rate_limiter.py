"""Unit tests for global rate limiter (fixed-window RPM admission).

Concerns: S1/S2 — rate-limit window accuracy.
Source: dermascan/shared/global_rate_limiter.py
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from dermascan.tests.fixtures.admission_helpers import wire_rate_limiter


@pytest.mark.unit
class TestGlobalRateLimiter:
    """Fixed-window counting, window rollover, and RPM=0 bypass."""

    def test_allows_up_to_rpm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S2: Exactly RPM requests allowed per window; (RPM+1) rejected."""
        rate_limiter = wire_rate_limiter(monkeypatch, fake_redis, rpm=5, window_s=60)

        allowed_count = 0
        for _ in range(5):
            allowed, remaining, retry_after = rate_limiter._consume_global_window(1)
            assert allowed is True
            allowed_count += 1

        denied, remaining, retry_after = rate_limiter._consume_global_window(1)
        assert denied is False
        assert remaining == 0
        assert retry_after >= 1
        assert allowed_count == 5

    def test_window_rollover_resets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S2: After window bucket changes, counter resets and requests are allowed again."""
        freezegun = pytest.importorskip("freezegun")
        rate_limiter = wire_rate_limiter(monkeypatch, fake_redis, rpm=2, window_s=60)

        with freezegun.freeze_time("2026-06-20 12:00:00"):
            assert rate_limiter._consume_global_window(1)[0] is True
            assert rate_limiter._consume_global_window(1)[0] is True
            assert rate_limiter._consume_global_window(1)[0] is False

        with freezegun.freeze_time("2026-06-20 12:01:00"):
            allowed, _, _ = rate_limiter._consume_global_window(1)
            assert allowed is True

    @pytest.mark.asyncio
    async def test_rpm_zero_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """S2: When _RPM <= 0, _enforce_global_rate_limit returns without touching Redis."""
        import dermascan.shared.global_rate_limiter as rate_limiter

        monkeypatch.setattr(rate_limiter, "_RPM", 0)
        await rate_limiter._enforce_global_rate_limit(cost=1)

    @pytest.mark.asyncio
    async def test_rejected_returns_retry_after(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_redis,
    ) -> None:
        """S1/S2: Over-RPM rejection raises 429 with Retry-After header."""
        rate_limiter = wire_rate_limiter(monkeypatch, fake_redis, rpm=1, window_s=60)
        await rate_limiter._enforce_global_rate_limit(cost=1)

        with pytest.raises(HTTPException) as exc_info:
            await rate_limiter._enforce_global_rate_limit(cost=1)

        assert exc_info.value.status_code == 429
        assert exc_info.value.headers is not None
        assert int(exc_info.value.headers["Retry-After"]) >= 1

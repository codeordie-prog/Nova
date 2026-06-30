"""Concurrency tests: rate limiter atomicity under parallel admission.

Concerns: C3 — rate-limiter atomicity (INCRBY pipeline).
Requires real Redis (testcontainers).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from dermascan.tests.fixtures.admission_helpers import wire_rate_limiter


@pytest.mark.concurrency
class TestRateLimiterRace:
    """M threads hammer one window; total allowed must equal RPM exactly."""

    def test_parallel_consume_never_exceeds_rpm(
        self,
        real_redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """C3: M parallel consumes → total allowed == RPM, zero over-admission."""
        rate_limiter = wire_rate_limiter(
            monkeypatch,
            real_redis,
            rpm=10,
            window_s=60,
            key_prefix="test:concurrency:rate",
        )
        thread_count: int = 40

        def consume_once() -> bool:
            allowed, _, _ = rate_limiter._consume_global_window(1)
            return allowed

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            results = list(executor.map(lambda _: consume_once(), range(thread_count)))

        assert sum(1 for allowed in results if allowed) == 10
        assert sum(1 for allowed in results if not allowed) == thread_count - 10

    def test_no_torn_reads_on_window_boundary(
        self,
        real_redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """C3: Concurrent requests at same window → count never exceeds RPM."""
        freezegun = pytest.importorskip("freezegun")
        rate_limiter = wire_rate_limiter(
            monkeypatch,
            real_redis,
            rpm=5,
            window_s=60,
            key_prefix="test:concurrency:boundary",
        )

        with freezegun.freeze_time("2026-06-20 15:00:00"):
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(rate_limiter._consume_global_window, 1) for _ in range(20)]
                outcomes = [future.result() for future in as_completed(futures)]

        allowed_total = sum(1 for allowed, _, _ in outcomes if allowed)
        assert allowed_total == 5

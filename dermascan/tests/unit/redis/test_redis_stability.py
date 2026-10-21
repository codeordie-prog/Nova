"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for Redis retry/backoff client.

Concerns: R4 — Redis transient failure; R5 — Redis hard failure; S7 — connection-pool ceiling.
Source: dermascan/shared/redis_stability.py
"""


from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from dermascan.shared import redis_stability


@pytest.mark.unit
class TestRunWithRedisRetries:
    """run_with_redis_retries backoff, jitter bounds, and exhaustion behavior."""

    def test_transient_failure_retries_then_succeeds(
        self,
        redis_stability_env: None,
    ) -> None:
        """R4: ConnectionError N-1 times then succeed → succeeds after retries."""
        calls: dict[str, int] = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RedisConnectionError("simulated transient blip")
            return "ok"

        result = redis_stability.run_with_redis_retries(
            flaky,
            operation_name="test_transient",
        )

        assert result == "ok"
        assert calls["n"] == 3

    def test_hard_failure_reraises_after_exhaustion(
        self,
        redis_stability_env: None,
    ) -> None:
        """R5: All retry attempts fail → original error re-raised."""
        calls: dict[str, int] = {"n": 0}

        def always_fails() -> str:
            calls["n"] += 1
            raise RedisConnectionError("simulated hard outage")

        with pytest.raises(RedisConnectionError, match="simulated hard outage"):
            redis_stability.run_with_redis_retries(
                always_fails,
                operation_name="test_hard_failure",
            )

        assert calls["n"] == 3

    def test_timeout_errors_are_retried(
        self,
        redis_stability_env: None,
    ) -> None:
        """R4 variant: TimeoutError is treated as transient and retried."""
        calls: dict[str, int] = {"n": 0}

        def flaky_timeout() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RedisTimeoutError("simulated read timeout")
            return "ok"

        result = redis_stability.run_with_redis_retries(
            flaky_timeout,
            operation_name="test_timeout",
        )

        assert result == "ok"
        assert calls["n"] == 2

    def test_explicit_attempt_override(
        self,
        redis_stability_env: None,
    ) -> None:
        """Call-level attempts= overrides env without changing global config."""
        calls: dict[str, int] = {"n": 0}

        def always_fails() -> str:
            calls["n"] += 1
            raise OSError("simulated network failure")

        with pytest.raises(OSError, match="simulated network failure"):
            redis_stability.run_with_redis_retries(
                always_fails,
                operation_name="test_override",
                attempts=2,
            )

        assert calls["n"] == 2


@pytest.mark.unit
class TestRetryDelay:
    """_retry_delay_s exponential backoff with bounded jitter."""

    def test_backoff_jitter_within_bounds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Delay stays within [exp_delay, exp_delay + 20% jitter]."""
        monkeypatch.setattr(redis_stability.random, "uniform", lambda _a, b: b)

        delay_attempt_0 = redis_stability._retry_delay_s(
            attempt_index=0,
            base_delay_s=0.25,
            max_delay_s=2.0,
        )
        assert delay_attempt_0 == pytest.approx(0.25 + 0.25 * 0.20)

        delay_attempt_high = redis_stability._retry_delay_s(
            attempt_index=10,
            base_delay_s=0.25,
            max_delay_s=2.0,
        )
        assert delay_attempt_high == pytest.approx(2.0 + 2.0 * 0.20)

    def test_backoff_grows_exponentially_before_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without jitter, delay doubles each attempt until max_delay_s."""
        monkeypatch.setattr(redis_stability.random, "uniform", lambda _a, _b: 0.0)

        assert redis_stability._retry_delay_s(
            attempt_index=0,
            base_delay_s=0.1,
            max_delay_s=1.0,
        ) == pytest.approx(0.1)
        assert redis_stability._retry_delay_s(
            attempt_index=1,
            base_delay_s=0.1,
            max_delay_s=1.0,
        ) == pytest.approx(0.2)
        assert redis_stability._retry_delay_s(
            attempt_index=2,
            base_delay_s=0.1,
            max_delay_s=1.0,
        ) == pytest.approx(0.4)


@pytest.mark.unit
class TestPoolExhaustion:
    """S7/L6: connection pool behavior under concurrency (integration-owned)."""

    def test_pool_exhaustion_bounded_wait(self) -> None:
        """S7/L6: Concurrency beyond max_connections → bounded wait, no deadlock."""
        pytest.skip(
            "Covered in integration/test_redis_connectivity.py — needs real Redis pool"
        )

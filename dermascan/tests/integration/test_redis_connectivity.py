"""Integration tests: real Redis connectivity via testcontainers.

Step 6 in redis_tests.md — confirms Docker + shared stability helpers against a live Redis 7 instance.
"""

from __future__ import annotations

import pytest

from dermascan.shared import redis_stability


@pytest.mark.integration
class TestRealRedisConnectivity:
    """Smoke tests against ephemeral Redis 7 (Docker required)."""

    def test_real_redis_ping(self, real_redis) -> None:
        """Container starts and responds to PING."""
        assert real_redis.ping() is True

    def test_check_redis_connectivity_uses_container(
        self,
        real_redis_url: str,
        reset_redis_client_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """check_redis_connectivity() succeeds when REDIS_URL points at the container."""
        monkeypatch.setenv("REDIS_URL", real_redis_url)
        redis_stability.check_redis_connectivity(decode_responses=True)

    def test_run_with_redis_retries_against_real_redis(
        self,
        real_redis_url: str,
        reset_redis_client_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retry wrapper works end-to-end against a live server (happy path)."""
        monkeypatch.setenv("REDIS_URL", real_redis_url)
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "2")

        client = redis_stability.get_redis_client(decode_responses=True, refresh=True)
        result = redis_stability.run_with_redis_retries(
            client.ping,
            operation_name="integration_ping",
        )

        assert result is True

    @pytest.mark.slow
    def test_small_pool_recycles_connections_sequentially(
        self,
        real_redis_url: str,
        reset_redis_client_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S7/L6: max_connections=2 serves many sequential PINGs by recycling connections."""
        monkeypatch.setenv("REDIS_URL", real_redis_url)
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "2")

        cfg = redis_stability.load_redis_stability_config()
        client = redis_stability.create_redis_client(cfg)

        for _ in range(8):
            assert client.ping() is True

    @pytest.mark.slow
    def test_small_pool_raises_when_exhausted_concurrently(
        self,
        real_redis_url: str,
        reset_redis_client_cache: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """S7/L6: redis-py fails fast with MaxConnectionsError — no indefinite hang."""
        import redis.exceptions as redis_exc

        monkeypatch.setenv("REDIS_URL", real_redis_url)
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "2")

        cfg = redis_stability.load_redis_stability_config()
        client = redis_stability.create_redis_client(cfg)
        pool = client.connection_pool
        held: list = []

        try:
            held.append(pool.get_connection())
            held.append(pool.get_connection())

            with pytest.raises(redis_exc.MaxConnectionsError):
                pool.get_connection()
        finally:
            for connection in held:
                pool.release(connection)

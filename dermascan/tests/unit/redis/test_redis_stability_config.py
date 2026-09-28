"""Unit tests for RedisStabilityConfig and client factory.

Source: dermascan/shared/redis_stability.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dermascan.shared import redis_stability


@pytest.mark.unit
class TestLoadRedisStabilityConfig:
    """Environment parsing and validation for resilient Redis settings."""

    def test_missing_redis_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REDIS_URL is required for any Redis connectivity."""
        monkeypatch.delenv("REDIS_URL", raising=False)

        with pytest.raises(RuntimeError, match="REDIS_URL is required"):
            redis_stability.load_redis_stability_config()

    def test_invalid_socket_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-positive socket timeouts are rejected at load time."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_S", "0")

        with pytest.raises(ValueError, match="socket timeout"):
            redis_stability.load_redis_stability_config()

    def test_invalid_retry_attempts_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retry attempts must be at least one."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "0")

        with pytest.raises(ValueError, match="RETRY_ATTEMPTS"):
            redis_stability.load_redis_stability_config()

    def test_load_config_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid env vars produce a fully populated RedisStabilityConfig."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/2")
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_S", "8")
        monkeypatch.setenv("REDIS_SOCKET_CONNECT_TIMEOUT_S", "4")
        monkeypatch.setenv("REDIS_HEALTH_CHECK_INTERVAL_S", "15")
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "50")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "5")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.5")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "3.0")
        monkeypatch.setenv("REDIS_SOCKET_KEEPALIVE", "false")
        monkeypatch.setenv("REDIS_RETRY_ON_TIMEOUT", "false")

        cfg = redis_stability.load_redis_stability_config(decode_responses=False)

        assert cfg.redis_url == "redis://127.0.0.1:6379/2"
        assert cfg.decode_responses is False
        assert cfg.socket_timeout_s == 8.0
        assert cfg.socket_connect_timeout_s == 4.0
        assert cfg.health_check_interval_s == 15
        assert cfg.max_connections == 50
        assert cfg.retry_attempts == 5
        assert cfg.retry_base_delay_s == 0.5
        assert cfg.retry_max_delay_s == 3.0
        assert cfg.socket_keepalive is False
        assert cfg.retry_on_timeout is False

    def test_pool_block_timeout_forever(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REDIS_POOL_BLOCK_TIMEOUT_S=none yields a blocking pool with no wait cap."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        monkeypatch.setenv("REDIS_POOL_BLOCK_TIMEOUT_S", "none")

        cfg = redis_stability.load_redis_stability_config()

        assert cfg.pool_blocking is True
        assert cfg.pool_block_timeout_s is None


@pytest.mark.unit
class TestCreateRedisClient:
    """create_redis_client passes stability settings into redis-py."""

    def test_create_redis_client_forwards_pool_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pool size, timeouts, and keepalive are forwarded to from_url."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        cfg = redis_stability.load_redis_stability_config()

        mock_pool = MagicMock()
        mock_client = MagicMock()
        pool_kwargs = {
            "decode_responses": cfg.decode_responses,
            "socket_timeout": cfg.socket_timeout_s,
            "socket_connect_timeout": cfg.socket_connect_timeout_s,
            "health_check_interval": cfg.health_check_interval_s,
            "socket_keepalive": cfg.socket_keepalive,
            "max_connections": cfg.max_connections,
        }
        with patch.object(
            redis_stability.BlockingConnectionPool,
            "from_url",
            return_value=mock_pool,
        ) as from_url:
            with patch.object(
                redis_stability.redis,
                "Redis",
                return_value=mock_client,
            ) as redis_ctor:
                client = redis_stability.create_redis_client(cfg)

        assert client is mock_client
        from_url.assert_called_once_with(
            cfg.redis_url,
            timeout=cfg.pool_block_timeout_s,
            **pool_kwargs,
        )
        redis_ctor.assert_called_once_with(
            connection_pool=mock_pool,
            retry_on_timeout=cfg.retry_on_timeout,
        )

    def test_create_redis_client_non_blocking_uses_from_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REDIS_POOL_BLOCKING=false keeps the legacy ConnectionPool via from_url."""
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        monkeypatch.setenv("REDIS_POOL_BLOCKING", "false")
        cfg = redis_stability.load_redis_stability_config()

        mock_client = MagicMock()
        with patch.object(redis_stability.redis.Redis, "from_url", return_value=mock_client) as from_url:
            client = redis_stability.create_redis_client(cfg)

        assert client is mock_client
        from_url.assert_called_once_with(
            cfg.redis_url,
            decode_responses=cfg.decode_responses,
            socket_timeout=cfg.socket_timeout_s,
            socket_connect_timeout=cfg.socket_connect_timeout_s,
            health_check_interval=cfg.health_check_interval_s,
            socket_keepalive=cfg.socket_keepalive,
            max_connections=cfg.max_connections,
            retry_on_timeout=cfg.retry_on_timeout,
        )

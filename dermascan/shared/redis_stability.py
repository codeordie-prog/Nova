"""
Author: Dermascan Team
Date: 2026-06-20
License: MIT
Description:
This module centralizes resilient Redis client construction and retry helpers.
It is intended to be imported by all services that communicate with Redis so
timeouts, retries, and connection-health behavior remain consistent.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_CLIENTS: dict[bool, redis.Redis] = {}


@dataclass(frozen=True)
class RedisStabilityConfig:
    """
    Redis client configuration with reliability defaults.

    Args:
        redis_url: Redis connection URL.
        decode_responses: Whether Redis responses are decoded to strings.
        socket_timeout_s: Read/write socket timeout in seconds.
        socket_connect_timeout_s: TCP connect timeout in seconds.
        health_check_interval_s: Periodic health-check interval in seconds.
        socket_keepalive: Whether OS keepalive is enabled.
        max_connections: Max size of Redis connection pool.
        retry_on_timeout: Whether timeout errors are retried by redis-py.
        retry_attempts: Number of operation-level retries in helper wrappers.
        retry_base_delay_s: Initial retry backoff in seconds.
        retry_max_delay_s: Maximum retry backoff in seconds.
    """

    redis_url: str
    decode_responses: bool = True
    socket_timeout_s: float = 10.0
    socket_connect_timeout_s: float = 5.0
    health_check_interval_s: int = 30
    socket_keepalive: bool = True
    max_connections: int = 200
    retry_on_timeout: bool = True
    retry_attempts: int = 4
    retry_base_delay_s: float = 0.25
    retry_max_delay_s: float = 2.0


def load_redis_stability_config(*, decode_responses: bool = True) -> RedisStabilityConfig:
    """
    Load resilient Redis configuration from environment variables.

    Args:
        decode_responses: Whether Redis payloads should be decoded to strings.

    Returns:
        RedisStabilityConfig: Parsed and validated runtime configuration.

    Raises:
        RuntimeError: If REDIS_URL is missing.
        ValueError: If one or more environment values are invalid.
    """

    redis_url: str = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for Redis connectivity")

    socket_timeout_s: float = float(os.getenv("REDIS_SOCKET_TIMEOUT_S", "10"))
    socket_connect_timeout_s: float = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT_S", "5"))
    health_check_interval_s: int = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_S", "30"))
    max_connections: int = int(os.getenv("REDIS_MAX_CONNECTIONS", "200"))
    retry_attempts: int = int(os.getenv("REDIS_OPERATION_RETRY_ATTEMPTS", "4"))
    retry_base_delay_s: float = float(os.getenv("REDIS_OPERATION_RETRY_BASE_S", "0.25"))
    retry_max_delay_s: float = float(os.getenv("REDIS_OPERATION_RETRY_MAX_S", "2.0"))
    socket_keepalive: bool = os.getenv("REDIS_SOCKET_KEEPALIVE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    retry_on_timeout: bool = os.getenv("REDIS_RETRY_ON_TIMEOUT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if socket_timeout_s <= 0 or socket_connect_timeout_s <= 0:
        raise ValueError("Redis socket timeout values must be > 0")
    if health_check_interval_s < 0:
        raise ValueError("REDIS_HEALTH_CHECK_INTERVAL_S must be >= 0")
    if max_connections < 1:
        raise ValueError("REDIS_MAX_CONNECTIONS must be >= 1")
    if retry_attempts < 1:
        raise ValueError("REDIS_OPERATION_RETRY_ATTEMPTS must be >= 1")
    if retry_base_delay_s <= 0 or retry_max_delay_s <= 0:
        raise ValueError("Redis retry delays must be > 0")

    return RedisStabilityConfig(
        redis_url=redis_url,
        decode_responses=decode_responses,
        socket_timeout_s=socket_timeout_s,
        socket_connect_timeout_s=socket_connect_timeout_s,
        health_check_interval_s=health_check_interval_s,
        socket_keepalive=socket_keepalive,
        max_connections=max_connections,
        retry_on_timeout=retry_on_timeout,
        retry_attempts=retry_attempts,
        retry_base_delay_s=retry_base_delay_s,
        retry_max_delay_s=retry_max_delay_s,
    )


def create_redis_client(config: RedisStabilityConfig) -> redis.Redis:
    """
    Create a Redis client with hardened connection behavior.

    Args:
        config: Resolved Redis stability configuration.

    Returns:
        redis.Redis: Configured Redis client instance.
    """

    return redis.Redis.from_url(
        config.redis_url,
        decode_responses=config.decode_responses,
        socket_timeout=config.socket_timeout_s,
        socket_connect_timeout=config.socket_connect_timeout_s,
        health_check_interval=config.health_check_interval_s,
        socket_keepalive=config.socket_keepalive,
        max_connections=config.max_connections,
        retry_on_timeout=config.retry_on_timeout,
    )


def get_redis_client(*, decode_responses: bool = True, refresh: bool = False) -> redis.Redis:
    """
    Return a cached resilient Redis client.

    Args:
        decode_responses: Whether Redis responses are decoded to strings.
        refresh: When True, recreate and replace the cached client.

    Returns:
        redis.Redis: Shared Redis client for the requested response mode.
    """

    global _CLIENTS
    if refresh or decode_responses not in _CLIENTS:
        cfg: RedisStabilityConfig = load_redis_stability_config(decode_responses=decode_responses)
        _CLIENTS[decode_responses] = create_redis_client(cfg)
    return _CLIENTS[decode_responses]


def _retry_delay_s(*, attempt_index: int, base_delay_s: float, max_delay_s: float) -> float:
    """
    Compute exponential backoff delay with bounded jitter.

    Args:
        attempt_index: Zero-based retry index.
        base_delay_s: Base delay in seconds.
        max_delay_s: Maximum backoff delay in seconds.

    Returns:
        float: Delay in seconds before next retry.
    """

    exp_delay: float = min(max_delay_s, base_delay_s * (2**attempt_index))
    jitter: float = random.uniform(0.0, exp_delay * 0.20)
    return exp_delay + jitter


def run_with_redis_retries(
    operation: Callable[[], T],
    *,
    operation_name: str,
    attempts: int | None = None,
    base_delay_s: float | None = None,
    max_delay_s: float | None = None,
) -> T:
    """
    Execute a Redis operation with retry/backoff for transient failures.

    Args:
        operation: Zero-argument callable that executes a Redis action.
        operation_name: Human-readable operation label for logs.
        attempts: Optional retry attempts override.
        base_delay_s: Optional base delay override.
        max_delay_s: Optional max delay override.

    Returns:
        T: The operation result when successful.

    Raises:
        RedisError: Re-raises the final Redis error after retries are exhausted.
        OSError: Re-raises non-Redis network-level failure after retries.
    """

    cfg: RedisStabilityConfig = load_redis_stability_config()
    max_attempts: int = attempts if attempts is not None else cfg.retry_attempts
    backoff_base_s: float = base_delay_s if base_delay_s is not None else cfg.retry_base_delay_s
    backoff_max_s: float = max_delay_s if max_delay_s is not None else cfg.retry_max_delay_s

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return operation()
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            last_error = exc
            is_last_attempt: bool = attempt == max_attempts - 1
            if is_last_attempt:
                logger.exception(
                    "redis operation failed after retries: op=%s attempts=%s",
                    operation_name,
                    max_attempts,
                )
                raise

            delay_s: float = _retry_delay_s(
                attempt_index=attempt,
                base_delay_s=backoff_base_s,
                max_delay_s=backoff_max_s,
            )
            logger.warning(
                "redis transient failure: op=%s attempt=%s/%s delay_s=%.3f err=%s",
                operation_name,
                attempt + 1,
                max_attempts,
                delay_s,
                exc,
            )
            time.sleep(delay_s)

    if last_error is not None:
        raise last_error
    raise RedisError(f"redis operation failed unexpectedly: op={operation_name}")


def check_redis_connectivity(*, decode_responses: bool = True) -> None:
    """
    Validate Redis connectivity by issuing PING with retries.

    Args:
        decode_responses: Whether to use decoded responses for the client.

    Raises:
        RedisError: If connectivity could not be verified.
        OSError: If network-level errors persist across retries.
    """

    client: redis.Redis = get_redis_client(decode_responses=decode_responses)
    run_with_redis_retries(
        lambda: client.ping(),
        operation_name="ping",
    )


__all__ = [
    "RedisStabilityConfig",
    "check_redis_connectivity",
    "create_redis_client",
    "get_redis_client",
    "load_redis_stability_config",
    "run_with_redis_retries",
]


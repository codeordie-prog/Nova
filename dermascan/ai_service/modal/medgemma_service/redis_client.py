"""
Author: Dermascan Team
Date: 2026-03-20
Description: Redis client for MedGemma service, including connection checking and client creation from environment variables.

REDIS schema:

Keys:
- Hash
- Stream

Values:
- medgemma:job:{job_id} - Hash with job metadata (job_id, status, started_at, created_at, finished_at, boot_id, text, attempts)
- medgemma:stream - Stream for job processing
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from redis import Redis

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()


@dataclass(frozen=True)
class RedisConfig:
    """
    Redis configuration dataclass for discrete host/port env vars.
    """

    host: str
    port: int
    username: str
    password: str
    db: int
    decode_responses: bool


class Client:
    """
    Redis client wrapper for MedGemma service.

    Attributes:
        config: Discrete Redis configuration when not using REDIS_URL.
        client: Redis client instance.
    """

    def __init__(self, client: Redis, *, config: RedisConfig | None = None) -> None:
        self.config = config
        self.client = client

    def get_redis(self) -> Redis:
        """
        Get the Redis client instance.

        Returns:
            Redis client instance.
        """
        return self.client


def _redis_db_from_env() -> int:
    raw = os.environ.get("REDIS_DB", "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _client_from_discrete_env() -> Client:
    """
    Build a Redis client from REDIS_HOST/PORT-style environment variables.

    Returns:
        Client: Wrapped Redis client.

    Raises:
        ValueError: If host or port is missing/invalid.
    """
    host = os.environ.get("REDIS_HOST", "").strip()
    port_raw = (os.environ.get("REDIS_PORT") or "").strip()
    if not host or not port_raw:
        raise ValueError("REDIS_HOST and REDIS_PORT are required when REDIS_URL is unset")

    return Client(
        Redis(
            host=host,
            port=int(port_raw),
            username=os.environ.get("REDIS_USERNAME", "default"),
            password=os.environ.get("REDIS_PASSWORD", ""),
            db=_redis_db_from_env(),
            decode_responses=True,
        ),
        config=RedisConfig(
            host=host,
            port=int(port_raw),
            username=os.environ.get("REDIS_USERNAME", "default"),
            password=os.environ.get("REDIS_PASSWORD", ""),
            db=_redis_db_from_env(),
            decode_responses=True,
        ),
    )


def redis_client_from_env() -> Client:
    """
    Create a Redis client from environment variables.

    Prefers ``REDIS_URL`` (same as backend workers; supports TLS via ``rediss://``).
    Falls back to discrete ``REDIS_HOST`` / ``REDIS_PORT`` for Modal deployments.

    Returns:
        Client: Wrapped Redis client.

    Raises:
        ValueError: If neither REDIS_URL nor discrete host/port configuration is valid.
        RuntimeError: If REDIS_URL is set but client creation fails.
    """
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        try:
            from dermascan.shared.redis_stability import get_redis_client

            return Client(get_redis_client(decode_responses=True))
        except Exception as exc:
            raise RuntimeError(f"Failed to create Redis client from REDIS_URL: {exc}") from exc

    return _client_from_discrete_env()


def redis_client_from_url() -> Redis:
    """
    Create a Redis client directly from REDIS_URL.

    Returns:
        Redis: Redis client instance.

    Raises:
        RuntimeError: If REDIS_URL is missing or client creation fails.
    """
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is not configured")
    try:
        return Redis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        raise RuntimeError(f"Failed to create Redis client from URL: {exc}") from exc


def check_redis_connection() -> None:
    """
    Check Redis connectivity using the configured client factory.

    Raises:
        redis.RedisError: If the ping fails.
    """
    redis_client_from_env().get_redis().ping()


if __name__ == "__main__":
    client = redis_client_from_env().get_redis()
    print(client.ping())
    print("Redis connection successful!")
    key = "medgemma:job:952817780a5946438ab91baab0f21e34"
    print(client.type(key))
    print(client.hgetall(key))
    print(client.xrange("medgemma:jobs"))

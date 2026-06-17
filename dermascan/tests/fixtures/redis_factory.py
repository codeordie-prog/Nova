"""Redis client factory helpers for unit and integration test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any


def make_fake_redis() -> Any:
    """Create an in-memory fakeredis client with Lua support.

    Returns:
        fakeredis.FakeStrictRedis instance.
    """
    import fakeredis

    return fakeredis.FakeStrictRedis(decode_responses=True)


def real_redis_session() -> Generator[Any, None, None]:
    """Yield a real Redis client from a testcontainers Redis 7 instance.

    Yields:
        redis.Redis client connected to the ephemeral container.
    """
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7") as container:
        yield container.get_client(decode_responses=True)

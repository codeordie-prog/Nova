"""Shared pytest fixtures for the Dermascan pre-deployment test suite.

See tests.md for fixture conventions and suite taxonomy.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import pytest

# Ryuk (testcontainers reaper) often fails on Windows Docker Desktop; disable by default.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture
def fake_redis() -> Any:
    """In-memory Redis (fakeredis) with Lua support for unit tests."""
    pytest.importorskip("fakeredis")
    import fakeredis

    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture(scope="session")
def real_redis_container() -> Generator[Any, None, None]:
    """Ephemeral Redis 7 container (session-scoped, Docker required)."""
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7") as container:
        yield container


@pytest.fixture(scope="session")
def real_redis_url(real_redis_container: Any) -> str:
    """Connection URL for the session-scoped Redis testcontainer."""
    host: str = real_redis_container.get_container_host_ip()
    port: int = int(real_redis_container.get_exposed_port(real_redis_container.port))
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def real_redis(real_redis_container: Any) -> Any:
    """Real Redis client via testcontainers for integration/concurrency suites."""
    return real_redis_container.get_client(decode_responses=True)


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> Any:
    """FastAPI TestClient with Redis and external deps patched."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from dermascan.backend.app.main import app

    # TODO: monkeypatch endpoints._redis / admission gates as needed
    _ = monkeypatch, fake_redis
    return TestClient(app)


@pytest.fixture
def reset_redis_client_cache() -> Generator[None, None, None]:
    """Clear cached Redis clients before and after integration tests."""
    from dermascan.shared import redis_stability

    redis_stability._CLIENTS.clear()
    yield
    redis_stability._CLIENTS.clear()


@pytest.fixture
def reclaim_env(real_redis) -> Generator[None, None, None]:
    """Wire reclaim_logic to the session Redis client."""
    from dermascan.backend.services.agents import reclaim_logic

    reclaim_logic.set_redis_client(real_redis)
    yield
    reclaim_logic.set_redis_client(None)


@pytest.fixture
def sample_images() -> dict[str, bytes]:
    """Valid jpeg/png/webp + corrupt + oversized byte payloads."""
    from dermascan.tests.fixtures.images import build_sample_images

    return build_sample_images()

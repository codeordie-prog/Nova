"""Shared pytest fixtures for the Dermascan pre-deployment test suite.

See tests.md for fixture conventions and suite taxonomy.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest


@pytest.fixture
def fake_redis() -> Any:
    """In-memory Redis (fakeredis) with Lua support for unit tests."""
    pytest.importorskip("fakeredis")
    import fakeredis

    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture(scope="session")
def real_redis() -> Generator[Any, None, None]:
    """Real Redis via testcontainers for integration/concurrency suites."""
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7") as container:
        yield container.get_client(decode_responses=True)


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
def sample_images() -> dict[str, bytes]:
    """Valid jpeg/png/webp + corrupt + oversized byte payloads."""
    from dermascan.tests.fixtures.images import build_sample_images

    return build_sample_images()

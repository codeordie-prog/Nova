"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Fixtures shared by Redis stability unit tests."""


from __future__ import annotations

from collections.abc import Generator

import pytest

from dermascan.shared import redis_stability


@pytest.fixture
def redis_stability_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Minimal env for redis_stability unit tests.

    Uses fast retry delays and disables real sleeps so tests stay under 50 ms.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
    monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.01")
    monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "0.05")
    monkeypatch.setattr(redis_stability.time, "sleep", lambda _: None)
    redis_stability._CLIENTS.clear()


@pytest.fixture
def reset_redis_client_cache() -> Generator[None, None, None]:
    """Clear cached Redis clients before and after a test."""
    redis_stability._CLIENTS.clear()
    yield
    redis_stability._CLIENTS.clear()

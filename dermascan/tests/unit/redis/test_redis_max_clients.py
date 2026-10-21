"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Tests for Redis max-clients error detection."""


from __future__ import annotations

from redis.exceptions import ConnectionError as RedisConnectionError

from dermascan.shared.redis_stability import is_redis_max_clients_error


def test_detects_max_clients_message() -> None:
    exc = RedisConnectionError("max number of clients reached")
    assert is_redis_max_clients_error(exc) is True


def test_ignores_generic_connection_error() -> None:
    exc = RedisConnectionError("Connection refused")
    assert is_redis_max_clients_error(exc) is False

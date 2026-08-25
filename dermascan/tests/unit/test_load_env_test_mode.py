"""Unit tests for TEST_MODE Redis redirection in load_env."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _load_load_env_module():
    """Load ``load_env.py`` directly for isolated unit tests."""
    module_path = Path(__file__).resolve().parents[2] / "shared" / "load_env.py"
    spec = importlib.util.spec_from_file_location(
        "dermascan_load_env_under_test",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _reset_env_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset load_env module state between tests."""
    load_env = _load_load_env_module()
    monkeypatch.setattr(load_env, "_ENV_LOADED", False)
    monkeypatch.delenv("TEST_MODE", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_TEST_URL", raising=False)
    monkeypatch.delenv("REDIS_TEST_HOST", raising=False)
    monkeypatch.delenv("REDIS_TEST_PORT", raising=False)
    monkeypatch.delenv("REDIS_TEST_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TEST_USERNAME", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TEST_BOT_TOKEN", raising=False)


def test_build_redis_test_url_from_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_redis_test_url() composes a URL from REDIS_TEST_* parts."""
    load_env = _load_load_env_module()
    monkeypatch.setenv("REDIS_TEST_USERNAME", "default")
    monkeypatch.setenv("REDIS_TEST_PASSWORD", "secret")
    monkeypatch.setenv("REDIS_TEST_HOST", "redis-test.example.com")
    monkeypatch.setenv("REDIS_TEST_PORT", "12345")

    assert (
        load_env.build_redis_test_url()
        == "redis://default:secret@redis-test.example.com:12345"
    )


def test_apply_test_mode_overrides_swaps_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_MODE=true redirects REDIS_URL without changing live settings in files."""
    load_env = _load_load_env_module()
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://live:pass@live.example.com:6379")
    monkeypatch.setenv(
        "REDIS_TEST_URL",
        "redis://default:testpass@test.example.com:1111",
    )

    load_env.apply_test_mode_overrides()

    assert os.environ["REDIS_URL"] == "redis://default:testpass@test.example.com:1111"


def test_resolve_redis_url_uses_live_when_test_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_redis_url() leaves live REDIS_URL when TEST_MODE is false."""
    load_env = _load_load_env_module()
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setenv("REDIS_URL", "redis://live:pass@live.example.com:6379")
    monkeypatch.setenv("REDIS_TEST_URL", "redis://default:testpass@test.example.com:1111")

    assert load_env.resolve_redis_url() == "redis://live:pass@live.example.com:6379"


def test_apply_test_mode_overrides_syncs_test_telegram_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST_MODE=true aligns TELEGRAM_BOT_TOKEN with TELEGRAM_TEST_BOT_TOKEN."""
    load_env = _load_load_env_module()
    monkeypatch.setenv("TEST_MODE", "true")
    monkeypatch.setenv("REDIS_TEST_URL", "redis://default:testpass@test.example.com:1111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "live-token")
    monkeypatch.setenv("TELEGRAM_TEST_BOT_TOKEN", "test-token")

    load_env.apply_test_mode_overrides()

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "test-token"

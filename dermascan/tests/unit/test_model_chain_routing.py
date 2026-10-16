"""Unit tests for model chain routing (Layer 0 primary vs alternate)."""

from __future__ import annotations

import pytest

from dermascan.backend.services.agents import model_chain_routing as routing
from dermascan.backend.services.agents.model_fallback_chain import should_fallback_to_next_model


@pytest.fixture(autouse=True)
def _redis_url_for_chain_routing_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_with_redis_retries`` loads backoff settings from ``REDIS_URL``."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")


class TestCancellationFallback:
    """499 / cancelled errors fall through within a chain."""

    def test_should_fallback_on_499_message(self) -> None:
        exc = RuntimeError(
            "Error calling model 'gemini-3.5-flash' (CANCELLED): 499 CANCELLED"
        )
        assert should_fallback_to_next_model(exc) is True


class TestGeminiDegradedRouting:
    """Redis-backed degraded mode and turn stickiness."""

    def test_begin_turn_uses_primary_when_healthy(self, fake_redis) -> None:
        mode = routing.begin_model_chain_turn(fake_redis, "req-primary")
        try:
            assert mode == routing.ChainMode.PRIMARY
            assert routing.resolve_chain_mode(fake_redis) == routing.ChainMode.PRIMARY
        finally:
            routing.end_model_chain_turn()

    def test_begin_turn_uses_alternate_when_degraded(self, fake_redis) -> None:
        fake_redis.setex(routing._REDIS_KEY_DEGRADED, 60, "1")
        mode = routing.begin_model_chain_turn(fake_redis, "req-alt")
        try:
            assert mode == routing.ChainMode.ALTERNATE
            sticky = fake_redis.get(routing._turn_sticky_key("req-alt"))
            assert sticky == routing.ChainMode.ALTERNATE.value
        finally:
            routing.end_model_chain_turn()

    def test_turn_sticky_survives_degraded_clear(self, fake_redis) -> None:
        fake_redis.setex(routing._REDIS_KEY_DEGRADED, 60, "1")
        routing.begin_model_chain_turn(fake_redis, "req-sticky")
        fake_redis.delete(routing._REDIS_KEY_DEGRADED)
        try:
            assert routing.resolve_chain_mode(fake_redis) == routing.ChainMode.ALTERNATE
        finally:
            routing.end_model_chain_turn()

    def test_notify_sets_degraded_after_threshold(self, fake_redis) -> None:
        exc = RuntimeError("504 DEADLINE_EXCEEDED")
        for _ in range(routing._error_threshold()):
            routing.notify_gemini_model_failure(
                fake_redis, "gemini:gemini-3.5-flash", exc
            )
        assert routing.is_gemini_degraded(fake_redis) is True

    def test_notify_ignores_openai_failures(self, fake_redis) -> None:
        exc = RuntimeError("504 gateway timeout")
        for _ in range(5):
            routing.notify_gemini_model_failure(
                fake_redis, "openai:gpt-4o-mini", exc
            )
        assert routing.is_gemini_degraded(fake_redis) is False

    def test_notify_counts_cancellation_on_gemini(self, fake_redis) -> None:
        exc = RuntimeError("499 CANCELLED")
        for _ in range(routing._error_threshold()):
            routing.notify_gemini_model_failure(
                fake_redis, "gemini:gemini-3.5-flash", exc
            )
        assert routing.is_gemini_degraded(fake_redis) is True


class TestRedisRetries:
    """Redis ops use run_with_redis_retries before fail-open fallbacks."""

    def test_is_gemini_degraded_uses_retry_wrapper(
        self, fake_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _fake_retry(operation, *, operation_name: str):
            calls.append(operation_name)
            return operation()

        monkeypatch.setattr(routing, "run_with_redis_retries", _fake_retry)
        assert routing.is_gemini_degraded(fake_redis) is False
        assert calls == ["model_chain:gemini_degraded_exists"]


class TestPreferPrimaryFlag:
    """MODEL_CHAIN_PREFER_PRIMARY forces alternate when off."""

    def test_resolve_uses_alternate_when_prefer_primary_off(
        self, fake_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEL_CHAIN_PREFER_PRIMARY", "false")
        assert routing.resolve_chain_mode(fake_redis) == routing.ChainMode.ALTERNATE

    def test_begin_turn_alternate_when_prefer_primary_off(
        self, fake_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEL_CHAIN_PREFER_PRIMARY", "off")
        mode = routing.begin_model_chain_turn(fake_redis, "req-test-alt")
        try:
            assert mode == routing.ChainMode.ALTERNATE
        finally:
            routing.end_model_chain_turn()


class TestChainSpecs:
    """Default alternate chain specs."""

    def test_orchestrator_alternate_includes_openai_mini_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ORCHESTRATOR_MODEL_CHAIN_ALT", raising=False)
        spec = routing.orchestrator_alternate_chain_spec()
        assert spec.startswith("openai:gpt-4o-mini")
        assert "gpt-4o," not in spec
        assert "gemini:gemini-3.5-flash" in spec

    def test_summarization_alternate_ends_with_gemini_lite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUMMARIZATION_MODEL_CHAIN_ALT", raising=False)
        spec = routing.summarization_alternate_chain_spec()
        assert "openai:gpt-5-mini" in spec
        assert spec.strip().endswith("gemini:gemini-3.1-flash-lite")
        assert "gemini-3.5" not in spec

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-10-16
License: MIT

Layer-0 model chain routing: primary (Gemini-first) vs alternate (OpenAI-first).

When Vertex Gemini is degraded (error signals in Redis), new turns prefer the
alternate chain spec. Turn stickiness keeps orchestrator steps and summarization
on the same chain for one request_id.
"""


from __future__ import annotations

import os
from contextvars import ContextVar
from enum import Enum
from collections.abc import Iterator
from typing import Any, Callable, Optional, TypeVar

from dermascan.backend.services.agents.model_fallback_chain import (
    ChainedChatModel,
    ModelChainRunner,
    is_provider_health_error,
    orchestrator_model_chain_spec,
    summarization_model_chain_spec,
)
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import run_with_redis_retries

logger = get_logger(__name__)

T = TypeVar("T")


def _redis_read_with_fallback(
    operation: Callable[[], T],
    *,
    operation_name: str,
    fallback: T,
) -> T:
    """
    Run a Redis read with retries; return ``fallback`` if all attempts fail.

    Args:
        operation: Zero-argument Redis read callable.
        operation_name: Label for retry logs.
        fallback: Value when Redis is unavailable after retries.

    Returns:
        T: Operation result or fallback.
    """
    try:
        return run_with_redis_retries(operation, operation_name=operation_name)
    except Exception:
        logger.exception("model chain routing: redis read failed op=%s", operation_name)
        return fallback


def _redis_write_best_effort(
    operation: Callable[[], T],
    *,
    operation_name: str,
) -> None:
    """
    Run a Redis write with retries; log and ignore if all attempts fail.

    Args:
        operation: Zero-argument Redis write callable.
        operation_name: Label for retry logs.

    Returns:
        None
    """
    try:
        run_with_redis_retries(operation, operation_name=operation_name)
    except Exception:
        logger.exception("model chain routing: redis write failed op=%s", operation_name)


_DEFAULT_ORCHESTRATOR_MODEL_CHAIN_ALT: str = (
    "openai:gpt-4o-mini,"
    "openai:gpt-5-mini,"
    "gemini:gemini-3.1-flash-lite,"
    "gemini:gemini-3.5-flash"
)

_DEFAULT_SUMMARIZATION_MODEL_CHAIN_ALT: str = (
    "openai:gpt-4o-mini,"
    "openai:gpt-5-mini,"
    "gemini:gemini-3.1-flash-lite"
)

_REDIS_KEY_DEGRADED: str = "model_chain:gemini_degraded"
_REDIS_KEY_ERROR_COUNT: str = "model_chain:gemini_error_count"

_turn_mode: ContextVar[Optional["ChainMode"]] = ContextVar("model_chain_turn_mode", default=None)


class ChainMode(str, Enum):
    """Active primary vs alternate chain selection."""

    PRIMARY = "primary"
    ALTERNATE = "alternate"


def orchestrator_alternate_chain_spec() -> str:
    """
    Return the OpenAI-first orchestrator chain spec from environment.

    Returns:
        str: Comma-separated provider:model list.
    """
    raw = os.getenv("ORCHESTRATOR_MODEL_CHAIN_ALT", _DEFAULT_ORCHESTRATOR_MODEL_CHAIN_ALT)
    return raw.strip()


def summarization_alternate_chain_spec() -> str:
    """
    Return the OpenAI-first summarization chain spec from environment.

    Returns:
        str: Comma-separated provider:model list.
    """
    raw = os.getenv("SUMMARIZATION_MODEL_CHAIN_ALT", _DEFAULT_SUMMARIZATION_MODEL_CHAIN_ALT)
    return raw.strip()


def _degraded_ttl_s() -> int:
    return max(60, int(os.getenv("MODEL_CHAIN_GEMINI_DEGRADED_TTL_S", "900")))


def _error_threshold() -> int:
    return max(1, int(os.getenv("MODEL_CHAIN_GEMINI_ERROR_THRESHOLD", "3")))


def _error_window_s() -> int:
    return max(10, int(os.getenv("MODEL_CHAIN_GEMINI_ERROR_WINDOW_S", "60")))


def _turn_sticky_ttl_s() -> int:
    return max(60, int(os.getenv("MODEL_CHAIN_TURN_STICKY_TTL_S", "3600")))


def prefer_primary_chain() -> bool:
    """
    Return True when the Gemini-first primary chain is the default playlist.

    When ``MODEL_CHAIN_PREFER_PRIMARY`` is off (``false`` / ``0`` / ``no``), the
    alternate (OpenAI-first) chain is used for every turn — useful for testing
    the alternate spec without waiting for Redis gemini_degraded.

    Returns:
        bool: False forces alternate chain as primary.
    """
    raw = os.getenv("MODEL_CHAIN_PREFER_PRIMARY", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _default_chain_mode(redis_client: Any) -> ChainMode:
    """
    Resolve chain mode when no turn context is bound.

    Args:
        redis_client: Redis client for degraded flag lookup.

    Returns:
        ChainMode: Primary or alternate.
    """
    if not prefer_primary_chain():
        return ChainMode.ALTERNATE
    if is_gemini_degraded(redis_client):
        return ChainMode.ALTERNATE
    return ChainMode.PRIMARY


def _turn_sticky_key(turn_id: str) -> str:
    return f"model_chain:turn:{turn_id}:mode"


def is_gemini_chain_model_key(model_key: str) -> bool:
    """
    Return True when the chain registry key refers to a Gemini model.

    Args:
        model_key: Breaker / chain name (e.g. ``gemini:gemini-3.5-flash``).

    Returns:
        bool: True for Gemini providers.
    """
    return model_key.strip().lower().startswith("gemini:")


def is_cancellation_error(exc: Exception) -> bool:
    """
    Detect client-side cancellation (HTTP 499 / CANCELLED).

    Args:
        exc: Raised exception from a model invocation.

    Returns:
        bool: True when the call was cancelled before completion.
    """
    for current in _iter_exception_chain(exc):
        text = str(current).lower()
        class_name = current.__class__.__name__.lower()
        if "cancelled" in class_name or "canceled" in class_name:
            return True
        if "499" in text and ("cancel" in text or "status" in text):
            return True
        if "status':" in text and "499" in text:
            return True
    return False


def is_gemini_degradation_error(exc: Exception) -> bool:
    """
    Return True when a Gemini failure should count toward fleet degraded mode.

    Args:
        exc: Raised exception from a model invocation.

    Returns:
        bool: True for provider-health errors and cancellations.
    """
    return is_provider_health_error(exc) or is_cancellation_error(exc)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_gemini_degraded(redis_client: Any) -> bool:
    """
    Return True when the fleet-wide Gemini degraded flag is set in Redis.

    Args:
        redis_client: Redis client with ``exists`` support.

    Returns:
        bool: True when alternate chain should be preferred for new turns.
    """
    return _redis_read_with_fallback(
        lambda: bool(redis_client.exists(_REDIS_KEY_DEGRADED)),
        operation_name="model_chain:gemini_degraded_exists",
        fallback=False,
    )


def resolve_chain_mode(redis_client: Any) -> ChainMode:
    """
    Resolve primary vs alternate chain for the current context.

    Turn stickiness (context var) wins; otherwise use the Redis degraded flag.

    Args:
        redis_client: Redis client for degraded flag lookup.

    Returns:
        ChainMode: Active chain mode.
    """
    bound = _turn_mode.get()
    if bound is not None:
        return bound
    return _default_chain_mode(redis_client)


def orchestrator_chain_spec_for_mode(mode: ChainMode) -> str:
    """
    Map chain mode to orchestrator chain spec string.

    Args:
        mode: Primary or alternate selection.

    Returns:
        str: Chain spec for ``build_model_chain_from_spec``.
    """
    if mode == ChainMode.ALTERNATE:
        return orchestrator_alternate_chain_spec()
    return orchestrator_model_chain_spec()


def summarization_chain_spec_for_mode(mode: ChainMode) -> str:
    """
    Map chain mode to summarization chain spec string.

    Args:
        mode: Primary or alternate selection.

    Returns:
        str: Chain spec for summarization middleware.
    """
    if mode == ChainMode.ALTERNATE:
        return summarization_alternate_chain_spec()
    return summarization_model_chain_spec()


def begin_model_chain_turn(redis_client: Any, turn_id: str) -> ChainMode:
    """
    Bind primary vs alternate chain for one orchestrator turn (request_id).

    Reads turn sticky key if present; otherwise derives from degraded flag and
    stores sticky mode for concurrent middleware on this turn.

    Args:
        redis_client: Redis client.
        turn_id: Ingress request id for the turn.

    Returns:
        ChainMode: Mode bound for this turn.
    """
    if not prefer_primary_chain():
        mode = ChainMode.ALTERNATE
        _turn_mode.set(mode)
        if turn_id:
            _redis_write_best_effort(
                lambda: redis_client.setex(
                    _turn_sticky_key(turn_id),
                    _turn_sticky_ttl_s(),
                    mode.value,
                ),
                operation_name="model_chain:turn_sticky_setex",
            )
        logger.info(
            "model chain turn bound: turn_id=%s mode=%s orchestrator_spec=%s "
            "(MODEL_CHAIN_PREFER_PRIMARY=off)",
            turn_id,
            mode.value,
            orchestrator_chain_spec_for_mode(mode),
        )
        return mode

    mode = ChainMode.PRIMARY
    if turn_id:
        sticky = _redis_read_with_fallback(
            lambda: redis_client.get(_turn_sticky_key(turn_id)),
            operation_name="model_chain:turn_sticky_get",
            fallback=None,
        )
        if sticky == ChainMode.ALTERNATE.value:
            mode = ChainMode.ALTERNATE
        elif sticky == ChainMode.PRIMARY.value:
            mode = ChainMode.PRIMARY
        else:
            mode = _default_chain_mode(redis_client)
            _redis_write_best_effort(
                lambda: redis_client.setex(
                    _turn_sticky_key(turn_id),
                    _turn_sticky_ttl_s(),
                    mode.value,
                ),
                operation_name="model_chain:turn_sticky_setex",
            )
    else:
        mode = _default_chain_mode(redis_client)

    _turn_mode.set(mode)
    logger.info(
        "model chain turn bound: turn_id=%s mode=%s orchestrator_spec=%s",
        turn_id,
        mode.value,
        orchestrator_chain_spec_for_mode(mode),
    )
    return mode


def end_model_chain_turn() -> None:
    """Clear turn-local chain mode after orchestrator invoke completes."""
    _turn_mode.set(None)


def notify_gemini_model_failure(redis_client: Any, model_key: str, exc: Exception) -> None:
    """
    Record a Gemini health failure and maybe set fleet degraded mode.

    Args:
        redis_client: Redis client.
        model_key: Chain model registry key.
        exc: Failure raised by the model call.

    Returns:
        None
    """
    if not is_gemini_chain_model_key(model_key):
        return
    if not is_gemini_degradation_error(exc):
        return
    def _increment_error_counter() -> int:
        count = int(redis_client.incr(_REDIS_KEY_ERROR_COUNT))
        if count == 1:
            redis_client.expire(_REDIS_KEY_ERROR_COUNT, _error_window_s())
        if count >= _error_threshold():
            redis_client.setex(_REDIS_KEY_DEGRADED, _degraded_ttl_s(), "1")
        return count

    try:
        count = run_with_redis_retries(
            _increment_error_counter,
            operation_name="model_chain:gemini_error_incr",
        )
        if count >= _error_threshold():
            logger.warning(
                "model chain: gemini degraded mode ON (errors=%s window_s=%s ttl_s=%s) "
                "last_model=%s exc=%s",
                count,
                _error_window_s(),
                _degraded_ttl_s(),
                model_key,
                exc.__class__.__name__,
            )
    except Exception:
        logger.exception(
            "model chain routing: notify failure failed model=%s", model_key
        )


class SelectiveModelChainRunner:
    """
    Delegate to primary or alternate ``ModelChainRunner`` based on routing mode.

    Args:
        primary: Gemini-first chain runner.
        alternate: OpenAI-first chain runner.
        redis_client: Redis client for mode resolution.
    """

    def __init__(
        self,
        *,
        primary: ModelChainRunner,
        alternate: ModelChainRunner,
        redis_client: Any,
    ) -> None:
        self._primary = primary
        self._alternate = alternate
        self._redis = redis_client

    @property
    def _model_chain(self) -> list[tuple[str, Any]]:
        """Expose active chain for logging compatibility."""
        return self._active()._model_chain

    def _active(self) -> ModelChainRunner:
        if resolve_chain_mode(self._redis) == ChainMode.ALTERNATE:
            return self._alternate
        return self._primary

    def invoke_middleware(
        self,
        request: Any,
        handler: Callable[..., Any],
    ) -> Any:
        """
        Invoke agent middleware on the active chain runner.

        Args:
            request: ModelRequest from agent middleware.
            handler: Next handler in the middleware stack.

        Returns:
            Any: First successful ModelResponse from the active chain.
        """
        return self._active().invoke_middleware(request, handler)

    def invoke_chat(
        self,
        input: Any,
        *,
        config: Optional[Any] = None,
        operation: str = "chat",
        **kwargs: Any,
    ) -> Any:
        """
        Invoke chat on the active chain runner.

        Args:
            input: Chat input for LangChain.
            config: Optional RunnableConfig.
            operation: Log label.
            **kwargs: Extra invoke kwargs.

        Returns:
            Any: Model response.
        """
        return self._active().invoke_chat(
            input,
            config=config,
            operation=operation,
            **kwargs,
        )


def build_selective_chained_chat_model(
    *,
    primary_runner: ModelChainRunner,
    alternate_runner: ModelChainRunner,
    redis_client: Any,
    label: str = "summarization",
) -> ChainedChatModel:
    """
    Build summarization middleware model that respects chain routing.

    Args:
        primary_runner: Primary summarization chain runner.
        alternate_runner: Alternate summarization chain runner.
        redis_client: Redis client for mode resolution.
        label: Operation label for logs.

    Returns:
        ChainedChatModel: Routed facade for SummarizationMiddleware.
    """
    selective = SelectiveModelChainRunner(
        primary=primary_runner,
        alternate=alternate_runner,
        redis_client=redis_client,
    )
    return ChainedChatModel(selective, label=label)

"""
Author: Dermascan Team
Date: 2026-07-07
Licence: MIT
Description:

Per-model LLM fallback chain with circuit breakers and full-chain retries.

Orchestrator middleware and report subagents share this module so each model
is tried in order (OpenAI variants, then Gemini variants) before giving up.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from langchain_openai import ChatOpenAI

from dermascan.backend.services.agents.build import build_gemini_chat_model
from dermascan.backend.services.agents.provider_circuit_breaker import (
    CircuitBreaker,
    load_model_circuit_breaker,
)
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

# Define the default model chain for the orchestrator and report agent
_DEFAULT_ORCHESTRATOR_MODEL_CHAIN: str = (
    "openai:gpt-4o,"
    "openai:gpt-5,"
    "gemini:gemini-3.5-flash,"
    "gemini:gemini-3.1-flash-lite"
)

# Cost-optimized: Gemini primary, OpenAI fallback for report subagents.
_DEFAULT_REPORT_MODEL_CHAIN: str = (
    "gemini:gemini-3.5-flash,"
    "gemini:gemini-3.1-flash-lite,"
    "openai:gpt-4o-mini,"
    "openai:gpt-4o"
)

# Lightweight Gemini-first chain for checkpoint summarization middleware.
_DEFAULT_SUMMARIZATION_MODEL_CHAIN: str = (
    "gemini:gemini-3.1-flash-lite,"
    "openai:gpt-4o-mini"
)

# Define the maximum number of retries and the delay between retries
MODEL_CHAIN_MAX_RETRIES: int = max(
    0, int(os.getenv("MODEL_CHAIN_MAX_RETRIES", "2"))
)
MODEL_CHAIN_RETRY_DELAY_S: float = max(
    1.0, float(os.getenv("MODEL_CHAIN_RETRY_DELAY_S", "10"))
)
# Subagent one-shot invoke retries (recursion, parse failures, exhausted model chain).
AGENT_INVOKE_MAX_RETRIES: int = max(
    0, int(os.getenv("AGENT_INVOKE_MAX_RETRIES", "3"))
)
AGENT_INVOKE_RETRY_DELAY_S: float = max(
    0.0, float(os.getenv("AGENT_INVOKE_RETRY_DELAY_S", "2"))
)
OPENAI_REASONING_MAX_TOKENS: int = max(
    1024, int(os.getenv("OPENAI_REASONING_MAX_TOKENS", "8192"))
)
OPENAI_REASONING_EFFORT: str = str(
    os.getenv("OPENAI_REASONING_EFFORT", "minimal")
).strip().lower()


def model_breaker_key(provider: str, model_id: str) -> str:
    """
    Build a stable circuit-breaker registry key for one model.

    Args:
        provider: Provider slug (``openai`` or ``gemini``).
        model_id: Provider model identifier.

    Returns:
        str: Breaker key, e.g. ``openai:gpt-4o``.
    """
    return f"{provider.strip().lower()}:{model_id.strip()}"


def parse_model_chain_spec(spec: str) -> list[tuple[str, str]]:
    """
    Parse a comma-separated ``provider:model_id`` chain specification.

    Args:
        spec: Chain string, e.g. ``openai:gpt-4o,gemini:gemini-3.5-flash``.

    Returns:
        list[tuple[str, str]]: Ordered ``(provider, model_id)`` pairs.

    Raises:
        ValueError: If any entry is malformed.
    """
    entries: list[tuple[str, str]] = []

    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"invalid model chain entry (expected provider:model): {item!r}")
        provider, model_id = item.split(":", 1)
        provider = provider.strip().lower()
        model_id = model_id.strip()
        if provider not in {"openai", "gemini"}:
            raise ValueError(f"unsupported provider in model chain: {provider!r}")
        if not model_id:
            raise ValueError(f"missing model id in chain entry: {item!r}")
        entries.append((provider, model_id))
    if not entries:
        raise ValueError("model chain spec is empty")
    return entries


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """
    Flatten an exception and its cause/context chain.

    Args:
        exc: Exception to flatten.

    Returns:
        list[BaseException]: Flattened exception chain.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        chain.append(cursor)
        seen.add(id(cursor))
        cursor = cursor.__cause__ or cursor.__context__
    return chain


def is_resource_exhaustion_error(exc: Exception) -> bool:
    """
    Return True when the failure looks like rate limiting or quota exhaustion.

    Args:
        exc: Exception to check.

    Returns:
        bool: True when the failure looks like rate limiting or quota exhaustion.
    """
    text: str = str(exc).lower()
    return "429" in text or "resource exhausted" in text or "rate limit" in text


def is_timeout_error(exc: Exception) -> bool:
    """
    Return True when the failure is timeout-related.

    Args:
        exc: Exception to check.

    Returns:
        bool: True when the failure is timeout-related.
    """
    for current in _iter_exception_chain(exc):
        if isinstance(current, TimeoutError):
            return True
        class_name: str = current.__class__.__name__.lower()
        text: str = str(current).lower()
        if "timeout" in class_name:
            return True
        if "timed out" in text or "timeout" in text or "deadline exceeded" in text:
            return True
    return False


def is_model_unavailable_error(exc: Exception) -> bool:
    """
    Return True when the model id is invalid or inaccessible for this account.

    Args:
        exc: Exception to check.

    Returns:
        bool: True when the model id is invalid or inaccessible for this account.
    """
    for current in _iter_exception_chain(exc):
        class_name: str = current.__class__.__name__.lower()
        text: str = str(current).lower()
        if "notfound" in class_name:
            return True
        if "model_not_found" in text:
            return True
        if "does not exist" in text and "model" in text:
            return True
        if "you do not have access" in text and "model" in text:
            return True
    return False


def should_fallback_to_next_model(exc: Exception) -> bool:
    """
    Decide whether a model failure should try the next model in the chain.

    Args:
        exc: Raised exception from a model invocation.

    Returns:
        bool: True when fall-through to the next model is appropriate.
    """
    if is_resource_exhaustion_error(exc) or is_timeout_error(exc):
        return True
    if is_model_unavailable_error(exc):
        return True
    if is_length_limit_error(exc):
        return True

    text: str = str(exc).lower()
    transient_markers: tuple[str, ...] = (
        "maximum context length",
        "context length",
        "input_tokens",
        "reduce the length",
        "timeout",
        "timed out",
        "connection",
        "connect error",
        "service unavailable",
        "temporarily unavailable",
        "bad gateway",
        "gateway timeout",
        "see other",
        "303",
        "502",
        "503",
        "504",
    )
    return any(marker in text for marker in transient_markers)


def _is_openai_reasoning_model(model_id: str) -> bool:
    """
    Return True for OpenAI models that bill hidden reasoning tokens from max_tokens.

    Args:
        model_id: OpenAI model slug.

    Returns:
        bool: True for gpt-5 / o-series reasoning models.
    """
    model_lower = model_id.strip().lower()
    if model_lower.startswith("o1") or model_lower.startswith("o3"):
        return True
    return model_lower.startswith("gpt-5") and "chat" not in model_lower


def is_length_limit_error(exc: Exception) -> bool:
    """
    Detect output-token truncation (reasoning models eating the full budget).

    Args:
        exc: Raised exception from a model invocation.

    Returns:
        bool: True when the model hit max output tokens before a parseable reply.
    """
    for current in _iter_exception_chain(exc):
        if current.__class__.__name__ == "LengthFinishReasonError":
            return True
        text: str = str(current).lower()
        if "length limit was reached" in text:
            return True
        if "lengthfinishreason" in text:
            return True
    return False


def is_recursion_error(exc: Exception) -> bool:
    """
    Detect LangGraph recursion-limit / tool-loop failures.

    Args:
        exc: Raised exception from an agent invocation.

    Returns:
        bool: True when the failure is recursion-related.
    """
    if exc.__class__.__name__ == "GraphRecursionError":
        return True
    return "recursion limit" in str(exc).lower()


def is_structured_output_error(exc: Exception) -> bool:
    """
    Detect structured-output / JSON parsing failures from agent responses.

    Args:
        exc: Raised exception from an agent invocation.

    Returns:
        bool: True when output parsing or validation failed.
    """
    for current in _iter_exception_chain(exc):
        class_name: str = current.__class__.__name__.lower()
        text: str = str(current).lower()
        if "validationerror" in class_name or "jsondecodeerror" in class_name:
            return True
        if "failed to parse" in text or "output parsing" in text:
            return True
        if "structured" in text and ("output" in text or "response" in text):
            return True
    return False


def is_tool_protocol_error(exc: Exception) -> bool:
    """
    Detect tool-call protocol mismatches (missing tool responses, bad tool_call_id).

    Args:
        exc: Raised exception from an agent invocation.

    Returns:
        bool: True when the conversation tool protocol is broken.
    """
    text: str = str(exc).lower()
    markers: tuple[str, ...] = (
        "tool_call_id",
        "tool_calls",
        "insufficient tool responses",
        "did not respond with a tool",
        "invalid parameter",
    )
    return any(marker in text for marker in markers)


def should_retry_agent_invoke(exc: Exception) -> bool:
    """
    Decide whether a full subagent invoke should be retried from scratch.

    Args:
        exc: Raised exception from ``agent.invoke``.

    Returns:
        bool: True when a clean retry may succeed (new agent, model chain re-walk).
    """
    if is_model_unavailable_error(exc):
        return False
    if is_recursion_error(exc):
        return True
    if is_structured_output_error(exc):
        return True
    if is_length_limit_error(exc):
        return True
    if is_tool_protocol_error(exc):
        return True
    return should_fallback_to_next_model(exc)


def build_model_chain_from_spec(
    *,
    chain_spec: str,
    openai_api_key: str,
    openai_temperature: float,
    openai_timeout_s: float,
    gemini_temperature: float,
    gemini_max_tokens: int,
    gemini_top_p: float,
    gemini_thinking_level: str,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str,
) -> list[tuple[str, Any]]:
    """
    Materialize LangChain chat models for each entry in a chain spec.

    Args:
        chain_spec: Comma-separated ``provider:model_id`` list.
        openai_api_key: OpenAI API key.
        openai_temperature: Sampling temperature for OpenAI models.
        openai_timeout_s: OpenAI request timeout seconds.
        gemini_temperature: Sampling temperature for Gemini models.
        gemini_max_tokens: Max output tokens for Gemini models.
        gemini_top_p: Top-p for Gemini models.
        gemini_thinking_level: Default Gemini thinking level.
        google_credentials_path: Vertex credentials path.
        google_project: GCP project id.
        google_location: GCP location.
        gemini_api_key: Gemini API key (non-Vertex mode).

    Returns:
        list[tuple[str, Any]]: Ordered ``(chain_name, chat_model)`` pairs.
    """
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY_QUTIS is required for the model chain")

    chain: list[tuple[str, Any]] = []
    for provider, model_id in parse_model_chain_spec(chain_spec):
        name = model_breaker_key(provider, model_id)
        if provider == "openai":
            openai_max_tokens = gemini_max_tokens
            model_kwargs: dict[str, Any] = {
                "model": model_id,
                "temperature": openai_temperature,
                "max_tokens": openai_max_tokens,
                "api_key": openai_api_key,
                "timeout": openai_timeout_s,
                "max_retries": 0,
            }
            if _is_openai_reasoning_model(model_id):
                openai_max_tokens = max(gemini_max_tokens, OPENAI_REASONING_MAX_TOKENS)
                model_kwargs["max_tokens"] = openai_max_tokens
                model_kwargs["reasoning_effort"] = OPENAI_REASONING_EFFORT
            chain.append((name, ChatOpenAI(**model_kwargs)))
            continue

        thinking = "minimal" if "lite" in model_id.lower() else gemini_thinking_level
        chain.append(
            (
                name,
                build_gemini_chat_model(
                    model=model_id,
                    temperature=gemini_temperature,
                    max_tokens=gemini_max_tokens,
                    top_p=gemini_top_p,
                    thinking_level=thinking,
                    google_credentials_path=google_credentials_path,
                    google_project=google_project,
                    google_location=google_location,
                    gemini_api_key=gemini_api_key,
                ),
            )
        )
    return chain


class ModelChainRunner:
    """
    Run an ordered model chain with per-model circuit breakers and full-chain retries.

    Args:
        model_chain: Ordered ``(model_name, chat_model)`` pairs.
        max_retries: Number of times to re-walk the full chain after all models fail.
        retry_delay_s: Seconds to wait between full-chain retries.
        record_attempt: Optional callback ``(model_name, success) -> None``.
    """

    def __init__(
        self,
        model_chain: list[tuple[str, Any]],
        *,
        max_retries: int = MODEL_CHAIN_MAX_RETRIES,
        retry_delay_s: float = MODEL_CHAIN_RETRY_DELAY_S,
        agent_max_retries: int = AGENT_INVOKE_MAX_RETRIES,
        agent_retry_delay_s: float = AGENT_INVOKE_RETRY_DELAY_S,
        record_attempt: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        if not model_chain:
            raise ValueError("model_chain must not be empty")
        self._model_chain = model_chain
        self._max_retries = max(0, max_retries)
        self._retry_delay_s = max(0.0, retry_delay_s)
        self._agent_max_retries = max(0, agent_max_retries)
        self._agent_retry_delay_s = max(0.0, agent_retry_delay_s)
        self._record_attempt = record_attempt
        self._breakers: dict[str, CircuitBreaker] = {
            name: load_model_circuit_breaker(name) for name, _ in model_chain
        }

    def invoke_middleware(
        self,
        request: Any,
        handler: Callable[..., Any],
    ) -> Any:
        """
        Try each model via LangChain agent middleware ``handler(request.override(model=))``.

        Args:
            request: ModelRequest from agent middleware.
            handler: Next handler in the middleware stack.

        Returns:
            Any: First successful ModelResponse.

        Raises:
            Exception: Last failure when all models and retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                logger.warning(
                    "model chain full retry %s/%s after all models failed; sleeping %.1fs",
                    attempt,
                    self._max_retries,
                    self._retry_delay_s,
                )
                time.sleep(self._retry_delay_s)

            try:
                return self._invoke_chain_once(request, handler)
            except Exception as exc:
                last_exc = exc
                if attempt >= self._max_retries or not should_fallback_to_next_model(exc):
                    raise

        raise last_exc if last_exc is not None else RuntimeError("model chain empty")

    def invoke_langchain_model(
        self,
        *,
        prompt: str,
        build_agent: Callable[[Any], Any],
        invoke_config: Optional[Any] = None,
        operation: str = "agent",
    ) -> dict[str, Any]:
        """
        Invoke a LangChain agent built per model for a one-shot prompt.

        Args:
            prompt: User prompt content.
            build_agent: Factory ``(chat_model) -> agent`` using fixed tools/prompt.
            invoke_config: Optional RunnableConfig for LangSmith/checkpointing.
            operation: Label for logs/errors.

        Returns:
            dict[str, Any]: Agent invoke response dict.

        Raises:
            RuntimeError: When all models and retries fail.
        """
        last_exc: Optional[Exception] = None
        max_agent_attempts = self._agent_max_retries + 1

        for agent_attempt in range(max_agent_attempts):
            if agent_attempt > 0:
                logger.warning(
                    "%s agent invoke retry %s/%s; sleeping %.1fs",
                    operation,
                    agent_attempt,
                    self._agent_max_retries,
                    self._agent_retry_delay_s,
                )
                if self._agent_retry_delay_s > 0:
                    time.sleep(self._agent_retry_delay_s)

            try:
                return self._invoke_langchain_model_once(
                    prompt=prompt,
                    build_agent=build_agent,
                    invoke_config=invoke_config,
                    operation=operation,
                )
            except Exception as exc:
                last_exc = exc
                if (
                    agent_attempt >= self._agent_max_retries
                    or not should_retry_agent_invoke(exc)
                ):
                    raise

        raise RuntimeError(f"{operation} failed: agent retries exhausted") from last_exc

    def _invoke_langchain_model_once(
        self,
        *,
        prompt: str,
        build_agent: Callable[[Any], Any],
        invoke_config: Optional[Any],
        operation: str,
    ) -> dict[str, Any]:
        """Walk the model chain with full-chain retries for one agent invoke."""
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                logger.warning(
                    "%s model chain full retry %s/%s; sleeping %.1fs",
                    operation,
                    attempt,
                    self._max_retries,
                    self._retry_delay_s,
                )
                time.sleep(self._retry_delay_s)

            try:
                return self._invoke_agents_once(
                    prompt=prompt,
                    build_agent=build_agent,
                    invoke_config=invoke_config,
                    operation=operation,
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= self._max_retries or not should_fallback_to_next_model(exc):
                    raise

        raise RuntimeError(f"{operation} failed: all models exhausted") from last_exc

    def invoke_chat(
        self,
        input: Any,
        config: Optional[Any] = None,
        *,
        operation: str = "chat",
        **kwargs: Any,
    ) -> Any:
        """
        Invoke chat models in chain order (for SummarizationMiddleware and similar).

        Args:
            input: Messages or prompt input accepted by underlying chat models.
            config: Optional RunnableConfig.
            operation: Label for logs.
            **kwargs: Forwarded to each model's ``invoke``.

        Returns:
            Any: First successful model response.

        Raises:
            Exception: Last failure when all models and retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                logger.warning(
                    "%s chain chat retry %s/%s; sleeping %.1fs",
                    operation,
                    attempt,
                    self._max_retries,
                    self._retry_delay_s,
                )
                time.sleep(self._retry_delay_s)
            try:
                return self._invoke_chat_once(
                    input,
                    config=config,
                    operation=operation,
                    **kwargs,
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= self._max_retries or not should_fallback_to_next_model(exc):
                    raise
        raise last_exc if last_exc is not None else RuntimeError(f"{operation}: chat chain empty")

    def _invoke_chat_once(
        self,
        input: Any,
        *,
        config: Optional[Any],
        operation: str,
        **kwargs: Any,
    ) -> Any:
        last_exc: Optional[Exception] = None
        chain_len = len(self._model_chain)

        for index, (name, model) in enumerate(self._model_chain):
            role = "primary" if index == 0 else f"fallback#{index}"
            is_last = index == chain_len - 1
            breaker = self._breakers[name]

            if not is_last and not breaker.allow():
                logger.warning(
                    "%s: model %s circuit OPEN (state=%s); skipping",
                    operation,
                    name,
                    breaker.state,
                )
                self._record(name, False)
                continue

            try:
                logger.info("%s: invoking chat model %s (%s)", operation, name, role)
                if config is not None:
                    response = model.invoke(input, config=config, **kwargs)
                else:
                    response = model.invoke(input, **kwargs)
                breaker.record_success()
                self._record(name, True)
                return response
            except Exception as exc:
                last_exc = exc
                breaker.record_failure()
                self._record(name, False)
                if is_last or not should_fallback_to_next_model(exc):
                    raise
                logger.warning(
                    "%s: chat model %s failed (%s); falling back",
                    operation,
                    name,
                    exc,
                )

        raise last_exc if last_exc is not None else RuntimeError(f"{operation}: chat chain empty")

    def _invoke_chain_once(self, request: Any, handler: Callable[..., Any]) -> Any:
        """
        Invoke the model chain once.
        Args:
            request: ModelRequest from agent middleware.
            handler: Next handler in the middleware stack.

        Returns:
            Any: First successful ModelResponse.

        Raises:
            Exception: Last failure when all models and retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        chain_len = len(self._model_chain)

        for index, (name, model) in enumerate(self._model_chain):
            role = "primary" if index == 0 else f"fallback#{index}"
            is_last = index == chain_len - 1
            breaker = self._breakers[name]

            if not is_last and not breaker.allow():
                logger.warning(
                    "model %s circuit OPEN (state=%s); skipping to next model",
                    name,
                    breaker.state,
                )
                self._record(name, False)
                continue

            try:
                logger.info("invoking model: %s (%s)", name, role)
                response = handler(request.override(model=model))
                logger.info("model replied: %s (%s)", name, role)
                breaker.record_success()
                self._record(name, True)
                return response

            except Exception as exc:
                last_exc = exc
                breaker.record_failure()
                self._record(name, False)
                if is_last or not should_fallback_to_next_model(exc):
                    raise
                logger.warning(
                    "model %s failed (%s); falling back to next model",
                    name,
                    exc,
                )

        raise last_exc if last_exc is not None else RuntimeError("model chain empty")

    def _invoke_agents_once(
        self,
        *,
        prompt: str,
        build_agent: Callable[[Any], Any],
        invoke_config: Optional[Any],
        operation: str,
    ) -> dict[str, Any]:
        """
        Invoke the agents once.
        Args:
            prompt: User prompt content.
            build_agent: Factory ``(chat_model) -> agent`` using fixed tools/prompt.
            invoke_config: Optional RunnableConfig for LangSmith/checkpointing.
            operation: Label for logs/errors.

        Returns:
            dict[str, Any]: Agent invoke response dict.

        Raises:
            Exception: Last failure when all models and retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        chain_len = len(self._model_chain)
        payload = {"messages": [{"role": "user", "content": prompt}]}

        for index, (name, model) in enumerate(self._model_chain):
            is_last = index == chain_len - 1
            breaker = self._breakers[name]

            if not is_last and not breaker.allow():
                logger.warning(
                    "%s: model %s circuit OPEN (state=%s); skipping",
                    operation,
                    name,
                    breaker.state,
                )
                continue

            try:
                agent = build_agent(model)
                if invoke_config is not None:
                    response = agent.invoke(payload, config=invoke_config)
                else:
                    response = agent.invoke(payload)
                breaker.record_success()
                return response if isinstance(response, dict) else {}
            except Exception as exc:
                last_exc = exc
                breaker.record_failure()
                if is_last or not should_fallback_to_next_model(exc):
                    raise
                logger.warning(
                    "%s: model %s failed (%s); trying next model",
                    operation,
                    name,
                    exc,
                )

        raise last_exc if last_exc is not None else RuntimeError(f"{operation}: model chain empty")

    def _record(self, model_name: str, success: bool) -> None:
        """
        Record the success or failure of a model invocation.
        Args:
            model_name: Name of the model.
            success: True if the model invocation was successful, False otherwise.
        """
        if self._record_attempt is not None:
            self._record_attempt(model_name, success)


class ChainedChatModel:
    """
    LangChain-compatible facade that routes ``invoke`` through a model chain.

    Used by SummarizationMiddleware where agent middleware is unavailable.
    Metadata (``_llm_type``, ``profile``, etc.) is delegated to the primary model.
    """

    def __init__(self, runner: ModelChainRunner, *, label: str = "summarization") -> None:
        self._runner = runner
        self._label = label
        self._primary_model = runner._model_chain[0][1]
        chain_names = [name for name, _ in runner._model_chain]
        self.model_name = chain_names[0] if chain_names else "chain"

    def __getattr__(self, name: str) -> Any:
        """Delegate LangChain model attributes to the chain primary model."""
        if name in {"_runner", "_label", "_primary_model"}:
            raise AttributeError(name)
        return getattr(self._primary_model, name)

    def invoke(
        self,
        input: Any,
        config: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke the first available chat model in the configured chain."""
        return self._runner.invoke_chat(
            input,
            config=config,
            operation=self._label,
            **kwargs,
        )

    async def ainvoke(
        self,
        input: Any,
        config: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """Async wrapper delegating to ``invoke`` (middleware compatibility)."""
        return self.invoke(input, config=config, **kwargs)

    def bind(self, **kwargs: Any) -> "ChainedChatModel":
        """Return self; chain routing happens at invoke time."""
        return self


def build_chained_chat_model(
    *,
    chain_spec: str,
    openai_api_key: str,
    openai_temperature: float,
    openai_timeout_s: float,
    gemini_temperature: float,
    gemini_max_tokens: int,
    gemini_top_p: float,
    gemini_thinking_level: str,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str,
    label: str = "summarization",
    max_retries: int = MODEL_CHAIN_MAX_RETRIES,
    retry_delay_s: float = MODEL_CHAIN_RETRY_DELAY_S,
) -> ChainedChatModel:
    """
    Build a chat model facade with Gemini-first or custom chain ordering.

    Args:
        chain_spec: Comma-separated ``provider:model_id`` list.
        openai_api_key: OpenAI API key.
        openai_temperature: OpenAI sampling temperature.
        openai_timeout_s: OpenAI request timeout seconds.
        gemini_temperature: Gemini sampling temperature.
        gemini_max_tokens: Max output tokens for Gemini models.
        gemini_top_p: Gemini top-p.
        gemini_thinking_level: Default Gemini thinking level.
        google_credentials_path: Vertex credentials path.
        google_project: GCP project id.
        google_location: GCP location.
        gemini_api_key: Gemini API key.
        label: Operation label for logs.
        max_retries: Full-chain retries after all models fail.
        retry_delay_s: Delay between full-chain retries.

    Returns:
        ChainedChatModel: Invoke facade for middleware use.
    """
    model_chain = build_model_chain_from_spec(
        chain_spec=chain_spec,
        openai_api_key=openai_api_key,
        openai_temperature=openai_temperature,
        openai_timeout_s=openai_timeout_s,
        gemini_temperature=gemini_temperature,
        gemini_max_tokens=gemini_max_tokens,
        gemini_top_p=gemini_top_p,
        gemini_thinking_level=gemini_thinking_level,
        google_credentials_path=google_credentials_path,
        google_project=google_project,
        google_location=google_location,
        gemini_api_key=gemini_api_key,
    )
    runner = ModelChainRunner(
        model_chain,
        max_retries=max_retries,
        retry_delay_s=retry_delay_s,
    )
    logger.info(
        "%s model chain: %s (chain_retries=%s, retry_delay_s=%s)",
        label,
        " -> ".join(name for name, _ in model_chain),
        max_retries,
        retry_delay_s,
    )
    return ChainedChatModel(runner, label=label)


def orchestrator_model_chain_spec() -> str:
    """Return the orchestrator model chain spec from environment."""
    return os.getenv("ORCHESTRATOR_MODEL_CHAIN", _DEFAULT_ORCHESTRATOR_MODEL_CHAIN).strip()


def report_model_chain_spec() -> str:
    """Return the report-agent model chain spec (Gemini primary, OpenAI fallback)."""
    return os.getenv("REPORT_MODEL_CHAIN", _DEFAULT_REPORT_MODEL_CHAIN).strip()


def summarization_model_chain_spec() -> str:
    """Return the summarization middleware model chain spec."""
    return os.getenv(
        "SUMMARIZATION_MODEL_CHAIN",
        _DEFAULT_SUMMARIZATION_MODEL_CHAIN,
    ).strip()

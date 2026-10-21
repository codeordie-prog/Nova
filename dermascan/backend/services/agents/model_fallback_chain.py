"""
Author: Dermascan Team
Date: 2026-07-07
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.
Description:

Per-model LLM fallback chain with circuit breakers and full-chain retries.

Orchestrator middleware and report subagents share this module so each model
is tried in order (Gemini variants first for orchestrator/reports, then OpenAI
fallback) before giving up."""


from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional, Union

from langchain.agents.middleware import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    wrap_model_call,
)
from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from dermascan.backend.services.agents.build import build_gemini_chat_model
from dermascan.backend.services.agents.provider_circuit_breaker import (
    CircuitBreaker,
    load_model_circuit_breaker,
)
from dermascan.shared.logger import get_logger
from dermascan.billing.meter import record_llm_usage
from dermascan.shared.vision_image_urls import (
    is_invalid_image_url_error,
    refresh_messages_vision_urls,
)

logger = get_logger(__name__)


class IncompleteStructuredOutputError(ValueError):
    """Raised when structured JSON parses but fails application validation."""

# Cost-optimized: Gemini primary, OpenAI fallback for orchestrator tool calling.
_DEFAULT_ORCHESTRATOR_MODEL_CHAIN: str = (
    "gemini:gemini-3.5-flash,"
    "gemini:gemini-3.1-flash-lite,"
    "openai:gpt-4o,"
    "openai:gpt-5"
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
_PROVIDER_MAX_RETRIES_HARD_CAP: int = 3
_DEFAULT_GEMINI_REQUEST_TIMEOUT_S: float = 45.0
GEMINI_REQUEST_TIMEOUT_S: float = max(
    1.0,
    float(os.getenv("GEMINI_REQUEST_TIMEOUT_S", str(_DEFAULT_GEMINI_REQUEST_TIMEOUT_S))),
)
GEMINI_MAX_RETRIES: int = max(
    1,
    min(
        _PROVIDER_MAX_RETRIES_HARD_CAP,
        int(os.getenv("GEMINI_MAX_RETRIES", "1")),
    ),
)

# Serialize tool calls to one per model step (OpenAI API flag + Gemini response trim).
AGENT_DISABLE_PARALLEL_TOOL_CALLS: bool = os.getenv(
    "AGENT_DISABLE_PARALLEL_TOOL_CALLS",
    os.getenv("ORCHESTRATOR_DISABLE_PARALLEL_TOOL_CALLS", "true"),
).strip().lower() in {"1", "true", "yes", "on"}
# Re-sign expired Firebase client-media URLs before each vision model call.
VISION_REFRESH_URLS_ENABLED: bool = os.getenv(
    "VISION_REFRESH_URLS_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}


def _is_openai_chat_model(model: Any) -> bool:
    """
    Return True when the bound model is LangChain OpenAI chat.

    Args:
        model: Model instance from a ModelRequest.

    Returns:
        bool: True for ChatOpenAI instances.
    """
    return isinstance(model, ChatOpenAI)


def apply_serial_tool_call_request(request: ModelRequest) -> ModelRequest:
    """
    Apply provider-specific request settings for serial tool calling.

    OpenAI accepts ``parallel_tool_calls=False`` via bind_tools kwargs.
    Gemini has no equivalent flag; response trimming handles it downstream.

    Args:
        request: Incoming model request.

    Returns:
        ModelRequest: Possibly overridden request.
    """
    if not AGENT_DISABLE_PARALLEL_TOOL_CALLS or not _is_openai_chat_model(request.model):
        return request
    return request.override(
        model_settings={
            **request.model_settings,
            "parallel_tool_calls": False,
        }
    )


def checkpoint_safe_structured_value(value: Any) -> Any:
    """
    Convert structured agent output to JSON-serializable data for Redis checkpoints.

    LangGraph's Redis checkpointer uses orjson and cannot persist live Pydantic
    models returned as ``structured_response`` on agent state.

    Args:
        value: Structured output (Pydantic model, dict, or other).

    Returns:
        Any: ``model_dump(mode="json")`` for Pydantic models; otherwise unchanged.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def structured_response_text(value: Any, *, field: str = "response") -> str | None:
    """
    Read a string field from invoke ``structured_response`` (dict or Pydantic).

    Args:
        value: ``structured_response`` from agent invoke state.
        field: Attribute or dict key to read.

    Returns:
        str | None: Field text, or None when missing or empty.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        raw = value.get(field)
        return str(raw).strip() if raw is not None else None
    if isinstance(value, BaseModel):
        raw = getattr(value, field, None)
        return str(raw).strip() if raw is not None else None
    return None


def checkpoint_safe_model_response(
    response: Union[ModelResponse, AIMessage, ExtendedModelResponse],
) -> Union[ModelResponse, AIMessage, ExtendedModelResponse]:
    """
    Ensure ``ModelResponse.structured_response`` is JSON-safe before checkpoint writes.

    Args:
        response: Middleware or model handler result.

    Returns:
        Same response type with ``structured_response`` converted when needed.
    """
    if isinstance(response, AIMessage):
        return response

    if isinstance(response, ExtendedModelResponse):
        inner = checkpoint_safe_model_response(response.model_response)
        if inner is response.model_response:
            return response
        return ExtendedModelResponse(
            model_response=inner,
            command=response.command,
        )

    structured = response.structured_response
    if structured is None:
        return response
    safe = checkpoint_safe_structured_value(structured)
    if safe is structured:
        return response
    return ModelResponse(result=response.result, structured_response=safe)


@wrap_model_call
def checkpoint_safe_structured_response_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], Any],
) -> Any:
    """
    Serialize structured output for Redis-backed LangGraph checkpoints.

    Use on agents with ``checkpointer=`` and ``response_format=`` (orchestrator,
    knowledge worker). Report subagents without a checkpointer should omit this.

    Args:
        request: Outbound model request.
        handler: Inner middleware / model handler.

    Returns:
        ModelResponse | AIMessage | ExtendedModelResponse: JSON-safe structured output.
    """
    response = handler(request)
    return checkpoint_safe_model_response(response)


def _truncate_ai_message_tool_calls(message: BaseMessage) -> BaseMessage:
    """
    Keep at most one tool call on an AI message.

    Args:
        message: Model output message.

    Returns:
        BaseMessage: Original message or a copy with extra tool calls removed.
    """
    if not isinstance(message, AIMessage):
        return message
    tool_calls = list(message.tool_calls or [])
    if len(tool_calls) <= 1:
        return message
    return message.model_copy(update={"tool_calls": tool_calls[:1]}, deep=True)


def enforce_serial_tool_call_response(
    response: Union[ModelResponse, AIMessage, ExtendedModelResponse],
) -> Union[ModelResponse, AIMessage, ExtendedModelResponse]:
    """
    Trim parallel tool calls from a model response to a single call.

    Args:
        response: Raw middleware/model handler result.

    Returns:
        Same response type with at most one tool call per AI message.
    """
    if not AGENT_DISABLE_PARALLEL_TOOL_CALLS:
        return response

    if isinstance(response, AIMessage):
        truncated = _truncate_ai_message_tool_calls(response)
        if truncated is not response and len(response.tool_calls or []) > 1:
            logger.warning(
                "truncated %s parallel tool calls to 1 (direct AIMessage response)",
                len(response.tool_calls) - 1,
            )
        return truncated

    if isinstance(response, ExtendedModelResponse):
        trimmed = enforce_serial_tool_call_response(response.model_response)
        if trimmed is response.model_response:
            return response
        return ExtendedModelResponse(
            model_response=trimmed,
            command=response.command,
        )

    truncated_messages = [_truncate_ai_message_tool_calls(msg) for msg in response.result]
    dropped = sum(
        max(0, len(getattr(msg, "tool_calls", []) or []) - 1)
        for msg in response.result
        if isinstance(msg, AIMessage) and len(msg.tool_calls or []) > 1
    )
    if dropped:
        logger.warning(
            "truncated %s parallel tool calls to 1 (ModelResponse)",
            dropped,
        )
    if truncated_messages == response.result:
        return response
    return ModelResponse(
        result=truncated_messages,
        structured_response=response.structured_response,
    )


def apply_vision_url_refresh_request(request: ModelRequest) -> ModelRequest:
    """
    Re-sign client-media image URLs in the outbound model request.

    LangGraph checkpoints replay prior multimodal turns with expired signed URLs.
    Refresh happens only on the ephemeral request; checkpoint state is unchanged.

    Args:
        request: Incoming model request.

    Returns:
        ModelRequest: Original or overridden request with fresh image URLs.
    """
    if not VISION_REFRESH_URLS_ENABLED:
        return request

    overrides: dict[str, Any] = {}
    refreshed_messages = refresh_messages_vision_urls(request.messages)
    if refreshed_messages is not request.messages:
        overrides["messages"] = refreshed_messages

    state = request.state
    state_messages = state.get("messages") if isinstance(state, dict) else None
    if isinstance(state_messages, list):
        refreshed_state_messages = refresh_messages_vision_urls(state_messages)
        if refreshed_state_messages is not state_messages:
            overrides["state"] = {**state, "messages": refreshed_state_messages}

    if not overrides:
        return request
    return request.override(**overrides)


@wrap_model_call
def refresh_vision_urls_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], Any],
) -> Any:
    """
    Refresh expired client-media signed URLs immediately before model invocation.

    Args:
        request: Model request about to invoke the LLM.
        handler: Next middleware/model handler.

    Returns:
        ModelResponse | AIMessage | ExtendedModelResponse: Handler result.
    """
    adjusted = apply_vision_url_refresh_request(request)
    return handler(adjusted)


@wrap_model_call
def serial_tool_calls_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], Any],
) -> Any:
    """
    Force at most one tool call per model step across providers.

    OpenAI: ``parallel_tool_calls=False`` on bind_tools.
    Gemini and others: keep only the first tool call in the model response.

    Args:
        request: Model request about to invoke the LLM.
        handler: Next middleware/model handler.

    Returns:
        ModelResponse | AIMessage | ExtendedModelResponse: Serial tool-call result.
    """
    adjusted = apply_serial_tool_call_request(request)
    response = handler(adjusted)
    return enforce_serial_tool_call_response(response)


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
    if is_structured_output_error(exc):
        return True
    if isinstance(exc, IncompleteStructuredOutputError):
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
        "499",
        "cancelled",
        "canceled",
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


def _bind_structured_schema(model: Any, schema: Any) -> Any:
    """
    Bind a Pydantic schema using native JSON-schema decoding when supported.

    Args:
        model: Chat model from the fallback chain.
        schema: Pydantic model or JSON schema.

    Returns:
        Any: Model bound for structured output.
    """
    try:
        return model.with_structured_output(schema, method="json_schema")
    except (TypeError, ValueError, NotImplementedError):
        return model.with_structured_output(schema)


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
    if is_invalid_image_url_error(exc):
        return True
    return should_fallback_to_next_model(exc)


def is_provider_health_error(exc: Exception) -> bool:
    """
    Return True only for failures that indicate the provider/endpoint is unhealthy.

    Circuit breakers must trip on provider-health signals (rate limits/quota,
    timeouts, connection errors, 5xx) and ignore request-shaped or permanent
    failures (recursion/tool loops, structured-output/JSON errors, output-length
    limits, tool-protocol mismatches, context-length overflows, and invalid model
    ids). Those are deterministic for the given input and must not blacklist a
    healthy model for all other concurrent traffic.

    Args:
        exc: Raised exception from a model or agent invocation.

    Returns:
        bool: True when the failure reflects provider unhealthiness.
    """
    # Request-shaped or permanent errors are out of scope for health tracking.
    if (
        is_recursion_error(exc)
        or is_structured_output_error(exc)
        or is_length_limit_error(exc)
        or is_tool_protocol_error(exc)
        or is_invalid_image_url_error(exc)
        or is_model_unavailable_error(exc)
    ):
        return False

    if is_resource_exhaustion_error(exc) or is_timeout_error(exc):
        return True

    health_markers: tuple[str, ...] = (
        "connection",
        "connect error",
        "service unavailable",
        "temporarily unavailable",
        "internal server error",
        "bad gateway",
        "gateway timeout",
        "overloaded",
        "500",
        "502",
        "503",
        "504",
        "499",
        "cancelled",
        "canceled",
    )
    for current in _iter_exception_chain(exc):
        class_name: str = current.__class__.__name__.lower()
        if "connecterror" in class_name or "connectionerror" in class_name:
            return True
        text: str = str(current).lower()
        # Context-length overflow is request-shaped even though it is transient-looking.
        if "context length" in text or "maximum context" in text or "reduce the length" in text:
            continue
        if any(marker in text for marker in health_markers):
            return True
    return False


def record_breaker_outcome(breaker: Any, exc: Exception) -> None:
    """
    Record a failed attempt against a circuit breaker only when provider-unhealthy.

    Request-shaped failures (recursion, structured output, length, tool protocol,
    context length, invalid model) are left neutral: the breaker is neither
    penalized nor reset, so one bad request cannot open a healthy model's circuit.

    Args:
        breaker: CircuitBreaker guarding the attempted model.
        exc: Exception raised by the attempt.

    Returns:
        None
    """
    if is_provider_health_error(exc):
        breaker.record_failure()


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
    gemini_timeout_s: float | None = None,
    gemini_max_retries: int | None = None,
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
        gemini_timeout_s: Per-request Gemini HTTP timeout; defaults to ``GEMINI_REQUEST_TIMEOUT_S``.
        gemini_max_retries: Google SDK retries; default ``1`` (chain handles fallback).

    Returns:
        list[tuple[str, Any]]: Ordered ``(chain_name, chat_model)`` pairs.
    """
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY_QUTIS is required for the model chain")

    resolved_gemini_timeout_s = (
        gemini_timeout_s if gemini_timeout_s is not None else GEMINI_REQUEST_TIMEOUT_S
    )
    resolved_gemini_max_retries = (
        gemini_max_retries if gemini_max_retries is not None else GEMINI_MAX_RETRIES
    )

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
                    request_timeout_s=resolved_gemini_timeout_s,
                    max_retries=resolved_gemini_max_retries,
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
        on_model_failure: Optional callback ``(model_name, exc) -> None`` after a
            failed model attempt (before fall-through or re-raise).
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
        on_model_failure: Optional[Callable[[str, Exception], None]] = None,
    ) -> None:
        if not model_chain:
            raise ValueError("model_chain must not be empty")
        self._model_chain = model_chain
        self._max_retries = max(0, max_retries)
        self._retry_delay_s = max(0.0, retry_delay_s)
        self._agent_max_retries = max(0, agent_max_retries)
        self._agent_retry_delay_s = max(0.0, agent_retry_delay_s)
        self._record_attempt = record_attempt
        self._on_model_failure = on_model_failure
        self._breakers: dict[str, CircuitBreaker] = {
            name: load_model_circuit_breaker(name) for name, _ in model_chain
        }

    def _emit_model_failure(self, model_name: str, exc: Exception) -> None:
        """
        Invoke optional failure hook for routing / observability.

        Args:
            model_name: Chain registry key for the model that failed.
            exc: Raised exception.

        Returns:
            None
        """
        if self._on_model_failure is not None:
            self._on_model_failure(model_name, exc)

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

    def invoke_structured_output(
        self,
        *,
        messages: Any,
        schema: Any,
        config: Optional[Any] = None,
        operation: str = "structured",
        post_parse: Optional[Callable[[Any], Any]] = None,
    ) -> Any:
        """
        Produce structured output across the model chain with no tools.

        Binds ``schema`` via ``model.with_structured_output`` and invokes each model
        in chain order with circuit breakers and full-chain retries. Because no tools
        are bound and no agent graph is created, this call cannot recurse
        (``GraphRecursionError`` is structurally impossible here).

        Args:
            messages: Message list or prompt input accepted by chat models.
            schema: Pydantic model / schema for structured output.
            config: Optional RunnableConfig for tracing.
            operation: Label for logs.
            post_parse: Optional validator/normalizer; may raise to try the next model.

        Returns:
            Any: Parsed structured output (instance of ``schema``).

        Raises:
            Exception: Last failure when all models and retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                logger.warning(
                    "%s structured chain retry %s/%s; sleeping %.1fs",
                    operation,
                    attempt,
                    self._max_retries,
                    self._retry_delay_s,
                )
                time.sleep(self._retry_delay_s)
            try:
                return self._invoke_structured_once(
                    messages=messages,
                    schema=schema,
                    config=config,
                    operation=operation,
                    post_parse=post_parse,
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= self._max_retries or not should_retry_agent_invoke(exc):
                    raise
        raise last_exc if last_exc is not None else RuntimeError(
            f"{operation}: structured chain empty"
        )

    def _invoke_structured_once(
        self,
        *,
        messages: Any,
        schema: Any,
        config: Optional[Any],
        operation: str,
        post_parse: Optional[Callable[[Any], Any]] = None,
    ) -> Any:
        """Walk the model chain once for a structured-output invoke."""
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
                logger.info("%s: invoking structured model %s (%s)", operation, name, role)
                structured_model = _bind_structured_schema(model, schema)
                if config is not None:
                    response = structured_model.invoke(messages, config=config)
                else:
                    response = structured_model.invoke(messages)
                if post_parse is not None:
                    response = post_parse(response)
                breaker.record_success()
                self._record(name, True)
                return response
            except Exception as exc:
                last_exc = exc
                record_breaker_outcome(breaker, exc)
                self._record(name, False)
                self._emit_model_failure(name, exc)
                if is_last or not should_fallback_to_next_model(exc):
                    raise
                logger.warning(
                    "%s: structured model %s failed (%s); falling back",
                    operation,
                    name,
                    exc,
                )

        raise last_exc if last_exc is not None else RuntimeError(
            f"{operation}: structured chain empty"
        )

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
                record_llm_usage(model_key=name, response=response, operation=operation)
                return response
            except Exception as exc:
                last_exc = exc
                record_breaker_outcome(breaker, exc)
                self._record(name, False)
                self._emit_model_failure(name, exc)
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
                record_llm_usage(model_key=name, response=response, operation="agent_step")
                return response

            except Exception as exc:
                last_exc = exc
                record_breaker_outcome(breaker, exc)
                self._record(name, False)
                self._emit_model_failure(name, exc)
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
                record_breaker_outcome(breaker, exc)
                self._emit_model_failure(name, exc)
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
    gemini_timeout_s: float | None = None,
    gemini_max_retries: int | None = None,
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
        gemini_timeout_s: Gemini HTTP timeout override.
        gemini_max_retries: Gemini SDK retry override.
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
        gemini_timeout_s=gemini_timeout_s,
        gemini_max_retries=gemini_max_retries,
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

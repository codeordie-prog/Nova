"""
Author: Dermascan Team
Date: 6-26-2026
Licence: MIT

A module for building agents.
"""



import os
import logging
from typing import Any, Final, Optional, Type

from google.oauth2 import service_account
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from dermascan.backend.services.agents.memory.redis_checkpointer import setup_from_url
from dermascan.shared.env_paths import resolve_env_path

_LOGGER: Final[logging.Logger] = logging.getLogger("dermascan.agents.build")



def build_checkpointer_url(url: str) -> Any:
    """
    Build a Checkpointer instance from a Redis URL.

    Args:
        url: Redis URL

    Returns:
        Checkpointer instance
    """
    if not url:
        raise RuntimeError("REDIS_URL is required for Redis checkpointing")
    saver, _ = setup_from_url(url)
    return saver


def open_checkpointer_url(url: str) -> tuple[Any, Any]:
    """
    Open a Redis-backed checkpointer and return (checkpointer, context_manager).

    Caller must close the returned context manager to release resources.
    """
    if not url:
        raise RuntimeError("REDIS_URL is required for Redis checkpointing")
    return setup_from_url(url)




def _use_vertex_ai() -> bool:
    """
    Return True when Gemini should authenticate via Vertex AI (OAuth/service account).

    Returns:
        bool: True when ``GOOGLE_GENAI_USE_VERTEXAI`` is enabled.
    """
    return os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_google_credentials(path: str) -> service_account.Credentials:
    """
    Google credentials from the specified path to service account credentials.

    Args:
        path: Path to the service account credentials file
    
    Returns:
        Google credentials
    """
    if not path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_QUTIS is required for Google provider")
    resolved_path = resolve_env_path(path, required=True)
    return service_account.Credentials.from_service_account_file(
        resolved_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def build_gemini_chat_model(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str = "",
    top_p: float | None = None,
    thinking_level: str | None = None,
    request_timeout_s: float | None = None,
    max_retries: int = 1,
) -> ChatGoogleGenerativeAI:
    """
    Build a Gemini chat model with the correct auth mode for the environment.

    Vertex AI (``GOOGLE_GENAI_USE_VERTEXAI=true``) requires service-account
    credentials and rejects API keys. Google AI Studio uses ``gemini_api_key``.

    Args:
        model: Gemini model name.
        temperature: Sampling temperature.
        max_tokens: Maximum **output** tokens per generation (visible reply +
            internal reasoning on Gemini 3.x). Does not include input/prompt tokens.
        google_credentials_path: Service-account JSON path for Vertex mode.
        google_project: GCP project ID for Vertex mode.
        google_location: GCP region for Vertex mode.
        gemini_api_key: Google AI Studio API key for non-Vertex mode.
        top_p: Optional nucleus sampling parameter.
        thinking_level: Gemini 3+ reasoning depth (``minimal``, ``low``, ``medium``,
            ``high``). Defaults to model default (``high`` on Gemini 3+).
        request_timeout_s: HTTP deadline for each generateContent call (seconds).
        max_retries: Google SDK retries; use ``1`` so model-chain fallback owns retries.

    Returns:
        ChatGoogleGenerativeAI: Configured Gemini client.

    Raises:
        RuntimeError: When required credentials are missing for the active mode.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if top_p is not None:
        kwargs["top_p"] = top_p
    if thinking_level is not None:
        kwargs["thinking_level"] = thinking_level
    if request_timeout_s is not None:
        kwargs["timeout"] = request_timeout_s
    kwargs["max_retries"] = max(1, max_retries)

    if _use_vertex_ai():
        kwargs["credentials"] = build_google_credentials(path=google_credentials_path)
        kwargs["project"] = google_project
        kwargs["location"] = google_location
    else:
        if not gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY_QUTIS is required when GOOGLE_GENAI_USE_VERTEXAI is disabled"
            )
        kwargs["api_key"] = gemini_api_key

    return ChatGoogleGenerativeAI(**kwargs)


_DEFAULT_SUMMARIZATION_GEMINI_MODEL: Final[str] = "gemini-3.1-flash-lite"
_DEFAULT_MAX_MODEL_LEN: Final[int] = 128000
_DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 2048
_MIN_SUMMARIZATION_TRIGGER_TOKENS: Final[int] = 1500
_MIN_RESERVED_OVERHEAD_TOKENS: Final[int] = 2048
_MIN_RESERVED_OUTPUT_TOKENS: Final[int] = 128


def compute_summarization_trigger_tokens(
    *,
    max_model_len: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    max_trigger_tokens: Optional[int] = None,
) -> int:
    """
    Compute SummarizationMiddleware trigger token budget from model context limits.

    Reserves output tokens and ~20% overhead before summarization kicks in, matching
    orchestrator worker budgeting.

    Args:
        max_model_len: Model context window in tokens (env fallback when omitted).
        max_output_tokens: Max generation tokens per turn (env fallback when omitted).
        max_trigger_tokens: Optional upper cap (e.g. OpenAI TPM / context budget).

    Returns:
        int: Token count at which summarization should trigger.
    """
    model_len = max_model_len or int(
        os.getenv(
            "AGENT_MAX_MODEL_LEN",
            os.getenv("ORCHESTRATOR_MAX_MODEL_LEN", str(_DEFAULT_MAX_MODEL_LEN)),
        )
    )
    output_tokens = max_output_tokens or int(
        os.getenv(
            "AGENT_MAX_TOKENS",
            os.getenv("ORCHESTRATOR_MAX_TOKENS", str(_DEFAULT_MAX_OUTPUT_TOKENS)),
        )
    )
    reserved_output_tokens = max(_MIN_RESERVED_OUTPUT_TOKENS, output_tokens)
    reserved_overhead_tokens = max(
        _MIN_RESERVED_OVERHEAD_TOKENS,
        int(model_len * 0.20),
    )
    trigger_tokens = max(
        _MIN_SUMMARIZATION_TRIGGER_TOKENS,
        model_len - reserved_output_tokens - reserved_overhead_tokens,
    )
    if max_trigger_tokens is not None:
        trigger_tokens = min(trigger_tokens, max_trigger_tokens)
    _LOGGER.info(
        "summarization budget: model_len=%s output_reserve=%s overhead_reserve=%s trigger_tokens=%s cap=%s",
        model_len,
        reserved_output_tokens,
        reserved_overhead_tokens,
        trigger_tokens,
        max_trigger_tokens,
    )
    return trigger_tokens


def build_summarization_gemini_model(
    *,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str = "",
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> ChatGoogleGenerativeAI:
    """
    Build a lightweight Gemini model for SummarizationMiddleware.

    Uses ``gemini-3.1-flash-lite`` by default (override via ``SUMMARIZATION_GEMINI_MODEL``).

    Args:
        google_credentials_path: Service-account JSON path for Vertex mode.
        google_project: GCP project ID for Vertex mode.
        google_location: GCP region for Vertex mode.
        gemini_api_key: Google AI Studio API key for non-Vertex mode.
        model: Optional model name override.
        max_tokens: Optional max output tokens for summaries.

    Returns:
        ChatGoogleGenerativeAI: Configured summarization client.
    """
    model_name = (
        model
        or os.getenv("SUMMARIZATION_GEMINI_MODEL", _DEFAULT_SUMMARIZATION_GEMINI_MODEL)
    ).strip()
    output_tokens = max_tokens or int(os.getenv("SUMMARIZATION_MAX_TOKENS", "1024"))
    gemini_timeout_raw = os.getenv("GEMINI_REQUEST_TIMEOUT_S", "").strip()
    if gemini_timeout_raw:
        summarization_timeout_s = float(gemini_timeout_raw)
    else:
        summarization_timeout_s = float(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "45"))
    return build_gemini_chat_model(
        model=model_name,
        temperature=0.0,
        max_tokens=output_tokens,
        google_credentials_path=google_credentials_path,
        google_project=google_project,
        google_location=google_location,
        gemini_api_key=gemini_api_key,
        top_p=0.95,
        request_timeout_s=summarization_timeout_s,
        max_retries=int(os.getenv("GEMINI_MAX_RETRIES", "1")),
    )


def build_agent(
    model: Any,
    tools: list,
    checkpointer: Any,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
    summarization_model: Optional[Any] = None,
    summarization_trigger_tokens: Optional[int] = None,
    max_model_len: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    summarization_keep_messages: int = 10,
) -> Any:
    """
    Build an agent with the specified model, tools, and checkpointer.
    
    Args:
        model: The model to use for the agent
        tools: List of tools to make available to the agent
        checkpointer: Checkpointer instance for state management
        system_prompt: Optional system prompt to guide the agent's behavior
        response_format: Optional response format to enforce structured output
        summarization_model: Optional model for SummarizationMiddleware (defaults to ``model``).
        summarization_trigger_tokens: Optional explicit trigger token budget.
        max_model_len: Model context window for trigger computation when trigger omitted.
        max_output_tokens: Max output tokens for trigger computation when trigger omitted.
        summarization_keep_messages: Recent messages retained after summarization.
    
    Returns:
        Configured agent instance
    """
    summary_model = summarization_model or model
    trigger_tokens = summarization_trigger_tokens or compute_summarization_trigger_tokens(
        max_model_len=max_model_len,
        max_output_tokens=max_output_tokens,
    )
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=[
            SummarizationMiddleware(
                model=summary_model,
                trigger=("tokens", trigger_tokens),
                keep=("messages", summarization_keep_messages),
            )
        ],
        checkpointer=checkpointer,
        response_format=response_format,
    )


def initialize_openai_agent(
    cfg: dict[str, Any],
    tools: list,
    checkpointer: Any,
    openai_api_key: str,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
) -> Any:
    """Initialize an OpenAI-backed agent."""
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI provider")

    model = ChatOpenAI(
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        verbose=True,
        api_key=openai_api_key,
    )
    return build_agent(
        model=model,
        tools=tools,
        checkpointer=checkpointer,
        system_prompt=system_prompt,
        response_format=response_format,
        max_output_tokens=int(cfg["max_tokens"]),
    )


def initialize_google_agent(
    cfg: dict[str, Any],
    tools: list,
    checkpointer: Any,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
) -> Any:
    """Initialize a Google-backed agent."""
    model = build_gemini_chat_model(
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        top_p=cfg["top_p"],
        google_credentials_path=google_credentials_path,
        google_project=google_project,
        google_location=google_location,
        gemini_api_key=gemini_api_key,
    )
    summarization_model = build_summarization_gemini_model(
        google_credentials_path=google_credentials_path,
        google_project=google_project,
        google_location=google_location,
        gemini_api_key=gemini_api_key,
    )
    return build_agent(
        model=model,
        tools=tools,
        checkpointer=checkpointer,
        system_prompt=system_prompt,
        response_format=response_format,
        summarization_model=summarization_model,
        max_output_tokens=int(cfg["max_tokens"]),
    )

"""
Author: Dermascan Team
Date: 2026-04-20
Description: This module implements the orchestrator worker.

Orchestrator agent serves as the entrypoint for the clients' inputs and coordinates the diagnosis process.
Its also the mouth of the system - The output point
Its autonous in a degree that its autonomically consuming entry jobs from the orchestrator stream, but also
it can be triggered by the signal stream to Proactively reach out to the client - This approach enables the system to 
Transcend the traditional structure of agentic system where a user prompt is required to initiate a response


"""

import os
import time
import logging
import json
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from typing import List, Dict, Any, Callable, Optional
from pydantic import BaseModel
import redis


from dermascan.backend.services.tools.orchestrator.orchestrator_tools import (
    cache_ingress_payload,
    orchestrator_tools,
    internet_search,
    wikipedia_search,
)
from dermascan.backend.services.agents.loader import load_agent_configurations
from dermascan.backend.services.agents.build import (
    open_checkpointer_url,
    build_gemini_chat_model,
    build_summarization_gemini_model,
    compute_summarization_trigger_tokens,
)
from dermascan.backend.services.agents.product_rules_signal_helper import (
    run_product_signal_rules_flow,
    run_skin_signal_rules_flow,
)
from dermascan.backend.services.agents.group_report_aggregator import (
    collect_and_compile_if_ready,
)
from dermascan.shared.logger import get_logger
from dermascan.shared.ingress_payload import resolve_image_urls_from_payload
from dermascan.shared.delivery_context import (
    format_delivery_context_for_prompt,
    load_delivery_context,
)
from dermascan.shared.intake_session_store import (
    IntakeSessionStoreError,
    archive_intake_session,
    load_archived_intake_session,
    load_intake_session,
)
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware, ToolCallLimitMiddleware, wrap_model_call, ModelRequest, ModelResponse
from langchain_openai import ChatOpenAI
from dermascan.backend.services.agents.prompts.orchestrator_prompt import system_prompt
from dermascan.backend.services.agents import reclaim_logic
from dermascan.backend.services.agents.provider_circuit_breaker import (
    CircuitBreaker,
    load_gemini_circuit_breaker,
    load_openai_circuit_breaker,
)
from dermascan.backend.services.agents.memory.redis_checkpointer import (
    delete_thread as delete_checkpoint_thread,
)
from dermascan.shared import agent_call_tracer
from dermascan.shared.langsmith_observability import (
    build_invoke_config,
    configure_worker_project,
    log_tracing_status,
)
from dermascan.shared.stream_pipeline_tracing import (
    enrich_stream_fields,
    extract_confidence_from_fields,
    extract_threshold_from_fields,
    log_pipeline_tracing_status,
    record_stream_produce,
    run_traced_stream_handler,
)


# Load environment variables
load_dotenv()
configure_worker_project("orchestrator")

# Get env variables
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY_QUTIS", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY_QUTIS", "")
CRED_PATH: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_QUTIS", "")
PROJECT_ID: str = os.getenv("GOOGLE_CLOUD_PROJECT_QUTIS", "qutisai")
# Gemini 3.x models require the global Vertex endpoint on this project.
LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
ORCHESTRATOR_GEMINI_MODEL: str = os.getenv(
    "ORCHESTRATOR_GEMINI_MODEL", "gemini-3.5-flash"
)
ORCHESTRATOR_GEMINI_THINKING_LEVEL: str = os.getenv(
    "ORCHESTRATOR_GEMINI_THINKING_LEVEL", "minimal"
)
REDIS_URL: str = os.getenv("REDIS_URL", "")
BASE_DIR: str = os.path.dirname(__file__)
CONFIG_PATH: str = os.path.join(BASE_DIR, "configurations", "agents_config.json")

# Orchestrator LLM chain: OpenAI GPT-4o (primary, tool calling) -> Gemini (fallback).
ORCHESTRATOR_MAX_MODEL_LEN: int = int(os.getenv("ORCHESTRATOR_MAX_MODEL_LEN", "128000"))
ORCHESTRATOR_MAX_TOKENS: int = int(os.getenv("ORCHESTRATOR_MAX_TOKENS", "2048"))
# Cap summarization trigger so OpenAI primary stays under typical TPM / context limits.
ORCHESTRATOR_OPENAI_SUMMARIZATION_TRIGGER_TOKENS: int = max(
    1500,
    int(os.getenv("ORCHESTRATOR_OPENAI_SUMMARIZATION_TRIGGER_TOKENS", "24000")),
)
_PROVIDER_MAX_RETRIES_HARD_CAP: int = 3
OPENAI_REQUEST_TIMEOUT_S: float = float(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "45"))
OPENAI_MAX_RETRIES: int = max(0, min(_PROVIDER_MAX_RETRIES_HARD_CAP, int(os.getenv("OPENAI_MAX_RETRIES", "2"))))
# Hard ceiling on agent reasoning/tool-call steps per turn.
AGENT_RECURSION_LIMIT: int = max(1, int(os.getenv("ORCHESTRATOR_AGENT_RECURSION_LIMIT", "12")))
ORCHESTRATOR_TOOL_RUN_LIMIT: int = max(3, int(os.getenv("ORCHESTRATOR_TOOL_RUN_LIMIT", "8")))
ORCHESTRATOR_GET_INTAKE_STATUS_RUN_LIMIT: int = max(
    1, int(os.getenv("ORCHESTRATOR_GET_INTAKE_STATUS_RUN_LIMIT", "1"))
)
# User-facing reply sent when a turn fails (e.g. recursion limit / provider
# error) so the client is never left without a response.
ORCHESTRATOR_FAILURE_REPLY: str = os.getenv(
    "ORCHESTRATOR_FAILURE_REPLY",
    "Sorry, I ran into a problem handling that. Please try again, and if you want a "
    "skin review, share a clear photo of the affected area.",
)
# When a turn fails with a recursion/loop error, reset the client's checkpoint so
# a poisoned conversation state cannot keep failing every subsequent message.
ORCHESTRATOR_RESET_CHECKPOINT_ON_RECURSION: bool = os.getenv(
    "ORCHESTRATOR_RESET_CHECKPOINT_ON_RECURSION", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# Define default configurations
DEFAULT_CONFIG: List[Dict[str, Any]] = [{
        "name": "orchestrator-google",
        "description": "Conversational Agent for orchestrating the diagnosis process",
        "model":"gemini-3.5-flash",
        "temperature": 0.0,
        "max_tokens": 1000,
        "top_p": 0.95,
        "top_k": 40
    },
    {
        "name": "orchestrator-openai",
        "description": "Conversational Agent for orchestrating the diagnosis process",
        "model":"gpt-4o",
        "temperature": 0.0,
        "max_tokens": 1000,
        "top_p": 0.95,
        "top_k": 40
    }
]

# Get logger
logger: logging.Logger = get_logger("agents")

# Stream settings
ORCHESTRATOR_JOB_STREAM: str = os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
ORCHESTRATOR_CONSUMER_GROUP: str = os.getenv("ORCHESTRATOR_CONSUMER_GROUP", "orchestrator-workers")
ORCHESTRATOR_SIGNAL_CONSUMER_GROUP: str = os.getenv("ORCHESTRATOR_SIGNAL_CONSUMER_GROUP", "orchestrator-signal-workers")
ORCHESTRATOR_CONSUMER_NAME_BASE: str = os.getenv("ORCHESTRATOR_CONSUMER_NAME", "orchestrator-worker")
ORCHESTRATOR_SIGNAL_CONSUMER_NAME_BASE: str = os.getenv("ORCHESTRATOR_SIGNAL_CONSUMER_NAME", "orchestrator-signal-worker")
ORCHESTRATOR_CONSUMER_NAME: str = f"{ORCHESTRATOR_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
ORCHESTRATOR_SIGNAL_CONSUMER_NAME: str = f"{ORCHESTRATOR_SIGNAL_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"

# Per-stream capacity / batching knobs.
# Two independent reader loops each have their own slot pool (semaphore) so a
# flood on one stream cannot starve the other. Block times encode the latency
# vs throughput asymmetry: signals block longer (they wake instantly on arrival)
# and read in small batches; jobs block shorter and read in larger batches.
JOB_MAX_INFLIGHT: int = max(1, int(os.getenv("JOB_MAX_INFLIGHT", "4")))
SIGNAL_MAX_INFLIGHT: int = max(1, int(os.getenv("SIGNAL_MAX_INFLIGHT")))
JOB_BATCH_MAX: int = max(1, int(os.getenv("JOB_BATCH_MAX", "16")))
SIGNAL_BATCH_MAX: int = max(1, int(os.getenv("SIGNAL_BATCH_MAX", "4")))
JOB_BLOCK_MS: int = int(os.getenv("JOB_BLOCK_MS", os.getenv("WORKERS_STREAM_BLOCK_MS", "500")))
SIGNAL_BLOCK_MS: int = int(os.getenv("SIGNAL_BLOCK_MS", "5000"))
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
ORCHESTRATOR_DLQ_STREAM: str = os.getenv("ORCHESTRATOR_DLQ_STREAM", "orchestrator:dlq")
ORCHESTRATOR_SIGNAL_DLQ_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_DLQ_STREAM", "orchestrator:signals:dlq")
OUTBOUND_STREAM: str = os.getenv("OUTBOUND_STREAM", "outbound:messages")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

# Proactive log settings and Conversational log settings.
# Per-client Redis list of completed signal-driven messages. The signal flow
# appends entries; the job flow reads recent entries and injects them into the
# user-facing prompt so the orchestrator knows what was already proactively
# sent. This implements the one-way merge: signals write, jobs read, no
# shared mutable state between flows.
PROACTIVE_LOG_KEY_PREFIX: str = os.getenv("PROACTIVE_LOG_KEY_PREFIX", "orchestrator:proactive_log")
PROACTIVE_LOG_MAX_LEN: int = max(1, int(os.getenv("PROACTIVE_LOG_MAX_LEN", "50")))
PROACTIVE_LOG_INCLUDE_N: int = max(0, int(os.getenv("PROACTIVE_LOG_INCLUDE_N", "5")))
PROACTIVE_LOG_TTL_SEC: int = int(os.getenv("PROACTIVE_LOG_TTL_SEC", "2592000"))  # 30 days
CONVERSATION_LOG_KEY_PREFIX: str = os.getenv("CONVERSATION_LOG_KEY_PREFIX", "orchestrator:conversation_log")
CONVERSATION_LOG_MAX_LEN: int = max(1, int(os.getenv("CONVERSATION_LOG_MAX_LEN", "100")))
CONVERSATION_LOG_INCLUDE_N: int = max(0, int(os.getenv("CONVERSATION_LOG_INCLUDE_N", "8")))
CONVERSATION_LOG_TTL_SEC: int = int(os.getenv("CONVERSATION_LOG_TTL_SEC", "2592000"))  # 30 days

# In-flight markers so the two reactive flows (job + signal) can detect when a
# peer agent invocation is already running for the same client. The job flow
# waits briefly for an in-flight signal to complete (so proactive_log is fresh
# before the job agent composes its reply). The signal flow does NOT wait on
# the job marker: signals are higher priority and should not be delayed by a
# user turn. Markers self-expire via TTL so a crashed worker cannot deadlock
# the peer flow indefinitely.
INFLIGHT_KEY_PREFIX: str = os.getenv("ORCHESTRATOR_INFLIGHT_KEY_PREFIX", "orchestrator:inflight")
INFLIGHT_TTL_SEC: int = max(5, int(os.getenv("ORCHESTRATOR_INFLIGHT_TTL_SEC", "30")))
INFLIGHT_WAIT_TIMEOUT_MS: int = max(0, int(os.getenv("ORCHESTRATOR_INFLIGHT_WAIT_TIMEOUT_MS", "3000")))
INFLIGHT_WAIT_POLL_MS: int = max(50, int(os.getenv("ORCHESTRATOR_INFLIGHT_WAIT_POLL_MS", "200")))

# Initialize REDIS client
_redis: redis.Redis = get_redis_client(decode_responses=True)

# Initialize reclaim_logic with the Redis client
reclaim_logic.set_redis_client(_redis)

# Process-wide reference to the active checkpointer, set in run_worker_forever.
# Used to reset a poisoned conversation thread on recursion failures.
_checkpointer: Any = None


def _reset_client_checkpoint(client_id: str) -> None:
    """
    Best-effort reset of a client's persisted conversation checkpoint.

    Args:
        client_id: Conversation thread identifier to clear.
    """
    if _checkpointer is None:
        return
    try:
        if delete_checkpoint_thread(_checkpointer, client_id):
            logger.warning("reset poisoned checkpoint for client_id=%s", client_id)
    except Exception:
        logger.exception("failed to reset checkpoint for client_id=%s", client_id)


def _is_recursion_error(exc: Exception) -> bool:
    """
    Detect LangGraph recursion-limit/loop failures.

    Args:
        exc: Raised exception from an agent invocation.

    Returns:
        bool: True when the failure is a recursion-limit error.
    """
    return "recursion limit" in str(exc).lower() or exc.__class__.__name__ == "GraphRecursionError"


def _strip_markdown_formatting(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\r\n", "\n", cleaned)
    return cleaned.strip()


class StructuredResponse(BaseModel):
    """
    Structured response model for the orchestrator agent.
    """
    response: str

def _default_cfg(provider: str) -> Dict[str, Any]:
    """
    Returns the default configuration for the given provider.
    
    Args:
        provider (str): The provider name (e.g., "google", "openai")
        
    Returns:
        Dict[str, Any]: The default configuration for the provider
    """
    normalized_provider = provider.strip().lower()
    return DEFAULT_CONFIG[0] if normalized_provider == "google" else DEFAULT_CONFIG[1]


# Function to get configuration file
def get_config_file(path: str, provider: str) -> Dict[str, Any]:
    """
    Gets the configuration file from the specified path or uses default if file not found

    Args:
        path (str): Path to the configuration file
        provider (str): Provider of the Base model one of ["google" | "openai"]
    
    Returns:
        Dict[str, Any]: Configuration file
    """
    try:
        logger.info("Loading configuration file...")
        cfg = load_agent_configurations(
            agent_type="orchestrator",
            path=path,
            provider=provider
        )

        return cfg

    except FileNotFoundError:
        logger.warning("Agents configurations file not found on the specified path, using defaults")
        return _default_cfg(provider)
    except Exception as e:
        logger.error("Failed to load agent configurations: %s", e, exc_info=True)
        raise


def _is_resource_exhaustion_error(exc: Exception) -> bool:
    """
    Checks if the exception is a resource exhaustion error.
    This will be used to decide when to switch to a different model provider.
    
    Args:
        exc (Exception): The exception to check
        
    Returns:
        bool: True if the exception is a resource exhaustion error, False otherwise
    """
    text: str = str(exc).lower()
    return "429" in text or "resource exhausted" in text or "rate limit" in text


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """
    Flatten an exception and its ``__cause__`` / ``__context__`` chain.

    Args:
        exc: Root exception.

    Returns:
        list[BaseException]: Root-first exception chain (deduplicated).
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        chain.append(cursor)
        seen.add(id(cursor))
        cursor = cursor.__cause__ or cursor.__context__
    return chain


def _is_timeout_error(exc: Exception) -> bool:
    """
    Detect timeout failures across wrapped SDK/network exceptions.

    Args:
        exc: Raised exception from provider invocation.

    Returns:
        bool: True when the failure is timeout-related.
    """
    for current in _iter_exception_chain(exc):
        # Direct timeout class checks first.
        if isinstance(current, TimeoutError):
            return True

        # Fallback to robust class/text heuristics for wrapped client errors.
        class_name: str = current.__class__.__name__.lower()
        text: str = str(current).lower()
        if "timeout" in class_name:
            return True
        if "timed out" in text or "timeout" in text or "deadline exceeded" in text:
            return True
    return False


def _should_fallback(exc: Exception) -> bool:
    """
    Decide whether a model failure warrants trying the next provider.

    Covers resource exhaustion (rate limits / quota) plus transient availability
    failures that are common with a self-hosted endpoint (cold start, scaledown,
    network blips, gateway errors). Logic/validation errors are NOT swallowed so
    genuine bugs still surface.

    Args:
        exc (Exception): The exception raised by a model invocation.

    Returns:
        bool: True if the orchestrator should fall through to the next model.
    """
    # Check 429 status code
    if _is_resource_exhaustion_error(exc):
        return True
    if _is_timeout_error(exc):
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


def _proactive_log_key(client_id: str) -> str:
    """
    Build the Redis key that stores a client's proactive-message log.

    Args:
        client_id: The client identifier the log is keyed on.

    Returns:
        The Redis list key.
    """
    return f"{PROACTIVE_LOG_KEY_PREFIX}:{client_id}"


def _conversation_log_key(client_id: str) -> str:
    """
    Build the Redis key that stores a client's conversation snapshot log.

    Args:
        client_id: The client identifier the log is keyed on.

    Returns:
        The Redis list key.
    """
    return f"{CONVERSATION_LOG_KEY_PREFIX}:{client_id}"


def _append_proactive_log(client_id: str, entry: dict[str, Any]) -> None:
    """
    Append a proactive-message entry to the client's log.

    Uses LPUSH (newest first) + LTRIM to enforce ``PROACTIVE_LOG_MAX_LEN``,
    then EXPIRE so the key auto-cleans for long-silent clients. Best-effort:
    failures are logged but never propagated, since logging must not block
    the signal flow's main path.

    Args:
        client_id: The client identifier the log is keyed on.
        entry: A JSON-serializable dict describing the proactive message.
    """
    key = _proactive_log_key(client_id)
    try:
        payload = json.dumps(entry, default=str)
        pipe = _redis.pipeline()
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, PROACTIVE_LOG_MAX_LEN - 1)
        pipe.expire(key, PROACTIVE_LOG_TTL_SEC)
        pipe.execute()
    except Exception:
        logger.exception("failed to append proactive log: client_id=%s", client_id)


def _finalize_intake_after_delivery(case_id: str, client_id: str) -> None:
    """
    Mark intake delivered and clear the active worksheet after outbound report.

    Args:
        case_id: Case identifier from the signal envelope.
        client_id: Client identifier from the signal envelope.
    """
    cid = str(client_id or "anonymous").strip() or "anonymous"
    case = str(case_id or "").strip()
    session = load_archived_intake_session(case, redis_client=_redis)
    if session is None:
        session = load_intake_session(cid, redis_client=_redis)
    if session is None:
        return
    try:
        archive_intake_session(session, redis_client=_redis, delete_active=True)
    except IntakeSessionStoreError:
        logger.exception(
            "failed to finalize intake after delivery: case_id=%s client_id=%s",
            case,
            cid,
        )


def _append_conversation_log(
    client_id: str,
    *,
    role: str,
    text: str,
    request_id: str,
    case_id: str,
) -> None:
    """
    Append one conversational turn to the client's conversation snapshot log.

    This log is read by signal-flow as read-only context to stay aware of the
    ongoing dialogue while preserving a separate checkpoint namespace.

    Args:
        client_id: The client identifier the log is keyed on.
        role: The role of the message (e.g., "user", "assistant").
        text: The text of the message.
        request_id: The request identifier.
        case_id: The case identifier.

    Returns:
        None
    """
    # Get body from text
    body = text.strip()
    if not body:
        return

    # Create Key for the conversational Log
    key = _conversation_log_key(client_id)

    # Create Entry for the Log
    entry = {
        "ts_ms": int(time.time() * 1000),
        "role": role,
        "text": body,
        "request_id": request_id,
        "case_id": case_id,
    }
    try:
        # Create a payload from the entry
        payload = json.dumps(entry, default=str)

        # Create a transaction pipeline
        pipe = _redis.pipeline()
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, CONVERSATION_LOG_MAX_LEN - 1)
        pipe.expire(key, CONVERSATION_LOG_TTL_SEC)
        pipe.execute()
    except Exception:
        logger.exception("failed to append conversation log: client_id=%s", client_id)


def _enqueue_outbound(
    *,
    client_id: str,
    case_id: str,
    channel: str,
    request_id: str,
    text: str,
    kind: str,
    must_deliver: bool,
    reply_to_message_id: Optional[str] = None,
    confidence: Optional[str] = None,
    threshold: Optional[str] = None,
) -> None:
    """
    Enqueue one outbound user-facing message for ordered channel delivery.

    Messages from job-flow and signal-flow both go through this lane so final
    dispatch can be serialized per client and ordered deterministically.

    Args:
        client_id: The client identifier the message is keyed on.
        case_id: The case identifier the message is keyed on.
        channel: The channel the message is sent to.
        request_id: The request identifier the message is keyed on.
        text: The message text.
        kind: The message kind.
        must_deliver: Whether the message must be delivered.
        reply_to_message_id: Optional Telegram message id to reply to.
    """
    body = text.strip()
    if not body:
        return

    priority = 10 if kind == "signal_update" else 20
    sequence = _redis.incr(f"outbound:seq:{client_id}")
    outbound_fields: dict[str, str] = {
        "client_id": client_id,
        "case_id": case_id,
        "channel": channel,
        "request_id": request_id,
        "kind": kind,
        "must_deliver": "true" if must_deliver else "false",
        "priority": str(priority),
        "created_at_ms": str(int(time.time() * 1000)),
        "seq": str(sequence),
        "text": body,
        "dedupe_key": f"{kind}:{request_id}:{case_id}",
        "reply_to_message_id": str(reply_to_message_id or ""),
    }
    if confidence:
        outbound_fields["confidence"] = confidence
    if threshold:
        outbound_fields["threshold"] = threshold
    stream_fields = enrich_stream_fields(outbound_fields)
    msg_id = str(
        _redis.xadd(
            OUTBOUND_STREAM,
            stream_fields,
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    )
    record_stream_produce(
        stream=OUTBOUND_STREAM,
        msg_id=msg_id,
        fields=stream_fields,
        extra_metadata={
            "worker": "orchestrator",
            "kind": kind,
            **({"confidence": confidence} if confidence else {}),
            **({"threshold": threshold} if threshold else {}),
        },
    )


def _read_proactive_log(client_id: str, limit: int) -> List[dict[str, Any]]:
    """
    Read the most recent proactive-message entries for a client.

    Args:
        client_id: The client identifier the log is keyed on.
        limit: Maximum number of entries to return (most recent first).

    Returns:
        A list of entry dicts, newest first. Empty if the key is missing,
        the read fails, or all entries are malformed.
    """
    if limit <= 0:
        return []
    key = _proactive_log_key(client_id)
    try:
        raw_entries = _redis.lrange(key, 0, limit - 1)
    except Exception:
        logger.exception("failed to read proactive log: client_id=%s", client_id)
        return []

    parsed: List[dict[str, Any]] = []
    for raw in raw_entries or []:
        try:
            parsed.append(json.loads(raw))
        except Exception:
            # Skip malformed entries silently; the log is best-effort context.
            continue
    return parsed


def _read_conversation_log(client_id: str, limit: int) -> List[dict[str, Any]]:
    """
    Read the most recent conversation turns for a client.

    Args:
        client_id: The client identifier the log is keyed on.
        limit: Maximum number of entries to return (most recent first).

    Returns:
        A list of conversational entries, newest first.
    """
    if limit <= 0:
        return []
    key = _conversation_log_key(client_id)
    try:
        raw_entries = _redis.lrange(key, 0, limit - 1)
    except Exception:
        logger.exception("failed to read conversation log: client_id=%s", client_id)
        return []

    parsed: List[dict[str, Any]] = []
    for raw in raw_entries or []:
        try:
            parsed.append(json.loads(raw))
        except Exception:
            continue
    return parsed


def _inflight_key(kind: str, client_id: str) -> str:
    """Build the Redis key for a per-client in-flight marker of the given kind."""
    return f"{INFLIGHT_KEY_PREFIX}:{kind}:{client_id}"


def _set_inflight(kind: str, client_id: str, payload: dict[str, Any]) -> None:
    """
    Record that a flow of the given kind is currently processing for this client.

    Uses a short TTL so a crashed worker cannot block the peer flow forever.
    Best-effort: failures are logged but never propagated.

    Args:
        kind: The flow kind (e.g., "signal", "job").
        client_id: The client identifier.
        payload: The payload to store.
    """
    try:
        _redis.set(
            _inflight_key(kind, client_id),
            json.dumps(payload, default=str),
            ex=INFLIGHT_TTL_SEC,
        )
    except Exception:
        logger.exception("inflight set failed: kind=%s client_id=%s", kind, client_id)


def _get_inflight(kind: str, client_id: str) -> Optional[dict[str, Any]]:
    """
    Read the current in-flight marker for (kind, client_id), or None if absent.
    
    Args:
        kind: The flow kind (e.g., "signal", "job").
        client_id: The client identifier.
        
    Returns:
        The in-flight payload or None if not present.
    """
    try:
        raw = _redis.get(_inflight_key(kind, client_id))
    except Exception:
        logger.exception("inflight get failed: kind=%s client_id=%s", kind, client_id)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _del_inflight(kind: str, client_id: str) -> None:
    """
    Clear the in-flight marker for (kind, client_id). Best-effort.
    Delete the inflight.

    Args:
        kind: The flow kind (e.g., "signal", "job").
        client_id: The client identifier.
    """
    try:
        _redis.delete(_inflight_key(kind, client_id))
    except Exception:
        logger.exception("inflight delete failed: kind=%s client_id=%s", kind, client_id)


def _wait_for_inflight(
    kind: str,
    client_id: str,
    *,
    timeout_ms: int = INFLIGHT_WAIT_TIMEOUT_MS,
    poll_ms: int = INFLIGHT_WAIT_POLL_MS,
) -> Optional[dict[str, Any]]:
    """
    Block briefly until the peer in-flight marker is cleared or timeout elapses.

    This is crucial in my current logic - This polling time allows a job to
    wait for a signal to update the proactive log before building the conversational
    prompt - This is part of the context engineering ingenuity to preserve semantic
    and logical flow on the client's side.

    (Remember the main key idea is keeping the stream coordination Assymetric - The Job
    has to wait for the Signal but the Signal should not wait for the Job)

    Returns the payload observed at entry if a marker was present (even if wait
    timed out), else None. Callers use the truthiness of the return value only
    as an indicator that a peer flow was racing and may have updated logs.
    """
    initial = _get_inflight(kind, client_id)
    if initial is None:
        return None

    deadline = time.time() + max(0, int(timeout_ms)) / 1000.0
    interval = max(50, int(poll_ms)) / 1000.0

    # While current time is within the deadline - Poll and wait
    while time.time() < deadline:
        time.sleep(interval)
        if _get_inflight(kind, client_id) is None:
            return initial

    logger.warning(
        "inflight wait timed out: kind=%s client_id=%s timeout_ms=%s",
        kind, client_id, timeout_ms,
    )
    return initial


# Build the orchestrator ingress context (text + optional multimodal image blocks)
def _build_orchestrator_text_context(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    username: Optional[str],
    payload: dict[str, Any],
    resolved_images: list[Any],
) -> str:
    """
    Build the text portion of the orchestrator user message.

    Args:
        request_id: Ingress request id for this turn.
        case_id: Case id.
        client_id: Client id.
        message_id: Source message id.
        channel: Ingress channel.
        username: Optional display name.
        payload: Parsed ingress payload object.
        resolved_images: Image attachments resolved for vision input.

    Returns:
        str: Text context for the orchestrator agent.
    """
    content = payload.get("content") or {}
    text: str = str(content.get("text") or "").strip()
    attachments: list = content.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []

    summary_lines = [
        "You are handling one orchestrator ingress job.",
        "Use tool calls when routing is needed.",
        f"request_id: {request_id}",
        f"case_id: {case_id}",
        f"client_id: {client_id}",
        f"message_id: {message_id}",
        f"channel: {channel}",
        f"username: {username}",
        f"text: {text or '<empty>'}",
        f"attachments_count: {len(attachments)}",
        f"resolved_image_count: {len(resolved_images)}",
    ]

    if resolved_images:
        summary_lines.append(
            "Vision hint: image block(s) are attached after this text. "
            "Inspect each image before classifying it or deciding routing."
        )
        summary_lines.append(
            "ingress_images_for_this_message: "
            + json.dumps(
                [
                    {
                        "attachment_id": image.attachment_id,
                        "index": image.index,
                        "mime_type": image.mime_type,
                    }
                    for image in resolved_images
                ],
                default=str,
            )
        )
    else:
        summary_lines.append("full_payload_json:")
        summary_lines.append(json.dumps(payload, default=str))

    if text and attachments:
        summary_lines.append(
            "Routing hint: user provided both text and attachment(s). You may need to forward for diagnosis."
        )
    if text:
        summary_lines.append("Routing hint: text is available. You may need to just respond or check event progress.")
    if attachments and not text:
        summary_lines.append("Routing hint: attachment-only request. May be a follow-up image or new case.")
    if not text and not attachments:
        summary_lines.append("Routing hint: missing content; ask clarification.")

    intake_session = load_intake_session(client_id, redis_client=_redis)
    if intake_session is not None:
        summary_lines.append("intake_session_snapshot:")
        summary_lines.append(json.dumps(intake_session.model_dump(mode="json"), default=str))

    proactive_entries = _read_proactive_log(client_id, PROACTIVE_LOG_INCLUDE_N)
    if proactive_entries:
        summary_lines.append("proactive_messages_recently_sent_to_client:")
        summary_lines.append(json.dumps(proactive_entries, default=str))
        summary_lines.append(
            "Context hint: do not repeat or contradict the proactive messages above."
        )

    return "\n".join(summary_lines)


def _build_orchestrator_user_content(
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    username: Optional[str],
    payload_json: str,
) -> list[dict[str, Any]]:
    """
    Build multimodal user message content for the orchestrator agent.

    Args:
        request_id: Ingress request id.
        case_id: Case id.
        client_id: Client id.
        message_id: Message id.
        channel: Channel name.
        username: Optional username.
        payload_json: Raw ingress payload JSON string.

    Returns:
        list[dict[str, Any]]: LangChain/OpenAI-style content blocks (text + image_url).
    """
    try:
        payload: dict[str, Any] = json.loads(payload_json)
    except Exception:
        payload = {}

    resolved_images = resolve_image_urls_from_payload(payload, redis_client=_redis)
    text_context = _build_orchestrator_text_context(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        message_id=message_id,
        channel=channel,
        username=username,
        payload=payload,
        resolved_images=resolved_images,
    )

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text_context}]
    for image in resolved_images:
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": image.image_url},
            }
        )
    return blocks

# Create/ensure a consumer group for the job stream exists
def _ensure_consumer_group(stream: str = "orchestrator_job") -> None:
    """
    Create or ensure a consumer group exists for the orchestrator job stream.
    """
    try:
        if stream == "orchestrator_job":
            _redis.xgroup_create(ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("consumer group created: stream=%s group=%s", ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP)

        elif stream == "signal_job":
            _redis.xgroup_create(ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("consumer signal group created: stream=%s group=%s", ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP)
        else:
            raise ValueError(f"Unsupported stream consumer group kind: {stream}")
    except Exception as e:
        if "BUSYGROUP" in str(e):
            return
        raise


def _build_dynamic_agent(*, checkpointer: Any) -> Any:
    """
    Build a dynamic agent with an ordered model fallback chain.

    Priority: OpenAI GPT-4o (primary, including tool calls) -> Google Gemini.
    Each provider is guarded by a circuit breaker so repeated failures skip
    straight to the next provider during the cooldown window.

    Args:
        checkpointer: The conversation checkpointer to attach to the agent.

    Returns:
        Any: A LangChain agent backed by the fallback chain.

    Raises:
        RuntimeError: If OpenAI is not configured.
    """
    google_cfg = get_config_file(CONFIG_PATH, "google")
    openai_cfg = get_config_file(CONFIG_PATH, "openai")

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OpenAI primary provider required: set OPENAI_API_KEY_QUTIS"
        )

    gemini_model_name: str = ORCHESTRATOR_GEMINI_MODEL or str(google_cfg["model"])

    gemini_model = build_gemini_chat_model(
        model=gemini_model_name,
        temperature=google_cfg["temperature"],
        max_tokens=ORCHESTRATOR_MAX_TOKENS,
        top_p=google_cfg["top_p"],
        thinking_level=ORCHESTRATOR_GEMINI_THINKING_LEVEL,
        google_credentials_path=CRED_PATH,
        google_project=PROJECT_ID,
        google_location=LOCATION,
        gemini_api_key=GEMINI_API_KEY,
    )

    openai_model = ChatOpenAI(
        model=openai_cfg["model"],
        temperature=openai_cfg["temperature"],
        max_tokens=ORCHESTRATOR_MAX_TOKENS,
        api_key=OPENAI_API_KEY,
        timeout=OPENAI_REQUEST_TIMEOUT_S,
        # Avoid SDK-level 429 retries; middleware fails over to Gemini immediately.
        max_retries=0,
    )

    model_chain: List[tuple[str, Any]] = [
        (f"openai:{openai_cfg['model']}", openai_model),
        (f"gemini:{gemini_model_name}", gemini_model),
    ]
    primary_model: Any = openai_model

    logger.info(
        "orchestrator model chain: %s (gemini fallback thinking_level=%s, max_tokens=%s)",
        " -> ".join(name for name, _ in model_chain),
        ORCHESTRATOR_GEMINI_THINKING_LEVEL,
        ORCHESTRATOR_MAX_TOKENS,
    )

    provider_breakers: dict[str, CircuitBreaker] = {
        "openai": load_openai_circuit_breaker(),
        "gemini": load_gemini_circuit_breaker(),
    }

    @wrap_model_call
    def dynamic_model_selection(request: ModelRequest, handler) -> ModelResponse:
        """
        Try OpenAI first, then Gemini on transient failures.

        Each provider has a circuit breaker. While a circuit is open the
        middleware skips that provider (when a fallback exists) instead of
        retrying an unhealthy endpoint.

        Args:
            request: The model request.
            handler: The model handler.

        Returns:
            ModelResponse: The first successful model response.
        """
        last_exc: Optional[Exception] = None
        for index, (name, model) in enumerate(model_chain):
            role: str = "primary" if index == 0 else f"fallback#{index}"
            is_last: bool = index == len(model_chain) - 1
            provider_key: str = name.split(":", 1)[0]
            breaker: Optional[CircuitBreaker] = provider_breakers.get(provider_key)

            if breaker is not None and not is_last and not breaker.allow():
                logger.warning(
                    "%s circuit OPEN (state=%s); skipping to next provider",
                    provider_key,
                    breaker.state,
                )
                agent_call_tracer.record_model_attempt(provider=name, success=False)
                continue

            try:
                logger.info("invoking model: %s (%s)", name, role)
                response: ModelResponse = handler(request.override(model=model))
                logger.info("model replied: %s (%s)", name, role)
                if breaker is not None:
                    breaker.record_success()
                agent_call_tracer.record_model_attempt(provider=name, success=True)
                return response

            except Exception as exc:
                last_exc = exc
                if breaker is not None:
                    breaker.record_failure()
                agent_call_tracer.record_model_attempt(provider=name, success=False)

                if is_last or not _should_fallback(exc):
                    raise

                logger.warning(
                    "model %s failed (%s); falling back to next provider",
                    name,
                    exc,
                )
        raise last_exc if last_exc is not None else RuntimeError("model chain empty")

    summarization_trigger_tokens: int = compute_summarization_trigger_tokens(
        max_model_len=ORCHESTRATOR_MAX_MODEL_LEN,
        max_output_tokens=ORCHESTRATOR_MAX_TOKENS,
        max_trigger_tokens=ORCHESTRATOR_OPENAI_SUMMARIZATION_TRIGGER_TOKENS,
    )

    summarization_model = build_summarization_gemini_model(
        google_credentials_path=CRED_PATH,
        google_project=PROJECT_ID,
        google_location=LOCATION,
        gemini_api_key=GEMINI_API_KEY,
    )

    return create_agent(
        model=primary_model,
        tools=orchestrator_tools,
        system_prompt=system_prompt,
        middleware=[
            dynamic_model_selection,
            ToolCallLimitMiddleware(
                tool_name="get_intake_status",
                run_limit=ORCHESTRATOR_GET_INTAKE_STATUS_RUN_LIMIT,
                exit_behavior="continue",
            ),
            ToolCallLimitMiddleware(
                run_limit=ORCHESTRATOR_TOOL_RUN_LIMIT,
                exit_behavior="continue",
            ),
            SummarizationMiddleware(
                model=summarization_model,
                trigger=("tokens", summarization_trigger_tokens),
                keep=("messages", 6),
            ),
        ],
        checkpointer=checkpointer,
        response_format=StructuredResponse,
    )


def _reader_loop(
    *,
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[Any, str, dict[str, Any]], None],
    agent: Any,
    pool: ThreadPoolExecutor,
    capacity: threading.Semaphore,
    batch_max: int,
    block_ms: int,
    stop_event: threading.Event,
) -> None:
    """
    Generic reader loop for a single Redis stream.

    Two instances of this function run concurrently in dedicated threads (one
    for jobs, one for signals). Each instance has its own capacity semaphore
    so the two streams cannot starve each other.

    Backpressure model:
        - Acquire >= 1 slot before reading (blocks if pool is full).
        - Opportunistically grab more slots up to ``batch_max`` without waiting.
        - Read at most ``slots_held`` messages from Redis.
        - Submit one task per message; each worker releases its own slot
          in a finally clause (slot ownership transfers to the worker).
        - Any slots reserved but unused (e.g. read returned fewer messages)
          are released immediately.

    This replaces the previous "inflight set + wait()" pattern with a simpler
    semaphore-driven design that has no busy polling.

    Args:
        stream: Redis stream key to read from.
        group: Consumer group name.
        consumer: Unique consumer name within the group.
        handler: Callable invoked per message: ``handler(agent, msg_id, fields)``.
        agent: The orchestrator agent passed through to the handler.
        pool: Shared thread pool that executes handlers.
        capacity: Semaphore bounding concurrent in-flight handlers for this stream.
        batch_max: Maximum messages to read per iteration.
        block_ms: ``XREADGROUP`` block timeout in milliseconds.
        stop_event: Cooperative shutdown flag.
    """
    while not stop_event.is_set():
        # Block until at least one slot is free, then opportunistically grab more.
        capacity.acquire()
        slots_held = 1
        while slots_held < batch_max and capacity.acquire(blocking=False):
            slots_held += 1

        try:
            resp = run_with_redis_retries(
                lambda: _redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=slots_held,
                    block=block_ms,
                ),
                operation_name=f"orchestrator_xreadgroup_{stream}",
            )
        except Exception:
            # Transient Redis errors must not kill the reader. Release reserved
            # slots and retry on the next iteration.
            logger.exception("xreadgroup failed: stream=%s group=%s", stream, group)
            for _ in range(slots_held):
                capacity.release()
            continue

        # Flatten the response: xreadgroup returns [(stream, [(id, fields), ...])]
        messages = [
            (msg_id, fields)
            for _, batch in (resp or [])
            for msg_id, fields in batch
        ]

        # Return any reserved-but-unused slots to the pool.
        unused = slots_held - len(messages)
        for _ in range(unused):
            capacity.release()

        # Submit one task per message. Slot ownership now belongs to the worker;
        # it must release exactly once when the handler completes (success or fail).
        for msg_id, fields in messages:
            def _run(_mid: str = str(msg_id), _fields: dict[str, Any] = fields) -> None:
                try:
                    run_traced_stream_handler(
                        stream=stream,
                        group=group,
                        consumer=consumer,
                        msg_id=_mid,
                        fields=_fields,
                        handler=lambda: handler(agent, _mid, _fields),
                    )
                except Exception:
                    # Handlers are expected to handle their own errors (DLQ + ack).
                    # This is a last-resort guard so a bug cannot leak a slot.
                    logger.exception(
                        "unexpected handler failure: stream=%s msg_id=%s", stream, _mid
                    )
                finally:
                    capacity.release()

            pool.submit(_run)


def _start_reclaim_thread(
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[Any, str, dict[str, Any]], None],
    agent: Any,
    dlq_stream: Optional[str],
    stop_event: threading.Event,
) -> threading.Thread:
    """
    Start a background thread that periodically reclaims stale pending messages.

    Args:
        stream: Redis stream key.
        group: Consumer group name.
        consumer: Consumer name to reclaim messages to.
        handler: The message handler (takes agent, msg_id, fields).
        agent: The agent instance to pass to the handler.
        dlq_stream: DLQ stream for messages exceeding max reclaim attempts.
        stop_event: Cooperative shutdown flag.

    Returns:
        The started reclaim thread.
    """
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:
                # Create a closure that adapts the handler signature
                def _reclaim_handler(msg_id: str, fields: dict[str, Any]) -> None:
                    # Mark as processed before handling to ensure idempotency
                    if reclaim_logic.mark_processed(stream, msg_id):
                        try:
                            handler(agent, msg_id, fields)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                stream, msg_id,
                            )
                            # Mark as not processed so it can be retried
                            _redis.delete(reclaim_logic.processed_key(stream, msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=stream,
                    group=group,
                    consumer=consumer,
                    handler=_reclaim_handler,
                    dlq_stream=dlq_stream,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        stream, reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", stream)

            # Wait for the interval or stop event
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"orchestrator-reclaim-{stream}",
        daemon=True,
    )
    thread.start()
    return thread


def _process_stream_message(agent: Any, msg_id: str, fields: dict[str, Any]) -> None:
    """
    Process a message from the orchestrator job stream.

    Args:
        agent: The orchestrator agent
        msg_id: The message ID
        fields: The message fields
    """
    request_id = str(fields.get("request_id") or "")
    case_id = str(fields.get("case_id") or request_id)
    client_id = str(fields.get("client_id") or "anonymous")
    message_id = str(fields.get("message_id") or request_id)
    channel = str(fields.get("channel") or "whatsapp")
    username = str(fields.get("username") or "").strip()
    payload_json = str(fields.get("payload") or "{}")

    try:
        # If a signal flow is currently processing a proactive update for this
        # client, wait briefly so the proactive_log is refreshed before we
        # compose the user-facing reply. This prevents the job agent from
        # producing a message that contradicts or duplicates an imminent
        # signal-driven delivery.
        observed_signal = _wait_for_inflight("signal", client_id)
        if observed_signal is not None:
            logger.info(
                "job flow waited for peer signal: client_id=%s signal=%s",
                client_id, observed_signal.get("signal_type") or "unknown",
            )

        _set_inflight(
            "job",
            client_id,
            {
                "request_id": request_id,
                "case_id": case_id,
                "message_id": message_id,
                "channel": channel,
                "ts_ms": int(time.time() * 1000),
            },
        )

        user_content = _build_orchestrator_user_content(
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            message_id=message_id,
            channel=channel,
            username=username,
            payload_json=payload_json,
        )
        config: RunnableConfig = build_invoke_config(
            thread_id=client_id,
            checkpoint_ns="orchestrator",
            run_name="orchestrator:job",
            channel=channel,
            flow="job",
            client_id=client_id,
            request_id=request_id,
            case_id=case_id,
            message_id=message_id,
            recursion_limit=AGENT_RECURSION_LIMIT,
        )
        cache_ingress_payload(
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            message_id=message_id,
            channel=channel,
            payload=payload_json,
        )

        agent_call_tracer.start_turn(
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            channel=channel,
            flow="job",
        )
        res = agent.invoke(
            {"messages": [HumanMessage(content=user_content)]},
            config=config,
        )
        agent_call_tracer.set_tool_names(agent_call_tracer.extract_tool_names(res))
        agent_call_tracer.end_turn(status="ok")
        reply = (
            res.get("structured_response").response
            if isinstance(res, dict) and res.get("structured_response") is not None
            else str(res)
        )
        reply = _strip_markdown_formatting(reply)

        print(reply)
        logger.info("Orchestrator reply: %s", reply)

        try:
            payload_obj: dict[str, Any] = json.loads(payload_json)
        except Exception:
            payload_obj = {}
        user_text = str(((payload_obj.get("content") or {}).get("text") or "")).strip()

        _append_conversation_log(
            client_id,
            role="user",
            text=user_text,
            request_id=request_id,
            case_id=case_id,
        )
        _append_conversation_log(
            client_id,
            role="assistant",
            text=reply,
            request_id=request_id,
            case_id=case_id,
        )

        _enqueue_outbound(
            client_id=client_id,
            case_id=case_id,
            channel=channel,
            request_id=request_id,
            text=reply,
            kind="job_reply",
            must_deliver=False,
            reply_to_message_id=message_id,
        )

        _redis.xack(ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_JOB_STREAM, msg_id)

        reclaim_logic.mark_processed(ORCHESTRATOR_JOB_STREAM, msg_id)

    except Exception as exc:

        agent_call_tracer.end_turn(status="failed", error=str(exc))

        if ORCHESTRATOR_RESET_CHECKPOINT_ON_RECURSION and _is_recursion_error(exc):
            _reset_client_checkpoint(client_id)

        try:
            _enqueue_outbound(
                client_id=client_id,
                case_id=case_id,
                channel=channel,
                request_id=request_id,
                text=ORCHESTRATOR_FAILURE_REPLY,
                kind="job_reply",
                must_deliver=True,
                reply_to_message_id=message_id,
            )
        except Exception:
            logger.exception("failed to enqueue fallback reply: request_id=%s", request_id)

        _redis.xadd(
            ORCHESTRATOR_DLQ_STREAM,
            {
                "request_id": request_id,
                "client_id": client_id,
                "source_stream": ORCHESTRATOR_JOB_STREAM,
                "error": str(exc),
                "payload": payload_json,
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
        _redis.xack(ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_JOB_STREAM, msg_id)
        logger.exception("orchestrator job failed: request_id=%s client_id=%s", request_id, client_id)

    finally:
        _del_inflight("job", client_id)


def _build_signal_input(
    fields: dict[str, Any],
    conversation_entries: Optional[List[dict[str, Any]]] = None,
    research_context: Optional[dict[str, Any]] = None,
) -> str:
    """
    Build the input prompt for processing an internal orchestrator signal.
    
    Args:
        fields: Dictionary containing signal fields
        
    Returns:
        Formatted input string for the orchestrator agent
    """
    signal_payload = {
        "signal_type": str(fields.get("signal_type") or "unknown"),
        "signal_id": str(fields.get("signal_id") or ""),
        "request_id": str(fields.get("request_id") or ""),
        "case_id": str(fields.get("case_id") or ""),
        "client_id": str(fields.get("client_id") or "anonymous"),
        "source_event_id": str(fields.get("source_event_id") or ""),
        "payload": str(fields.get("payload") or ""),
        "notes": str(fields.get("notes") or ""),
    }
    lines = [
        "You are processing an internal orchestrator signal.",
        "This signal comes from an internal update of the case under processing.",
        "This signal is from an internal dermatological review; avoid definitive language.",
        "If the signal provided a diagnosis in the signal payload, state that diagnosis clearly to the user.",
        "Do not deny or withhold condition information from the internal signal payload.",
        "Use non-definitive framing, such as 'the model suggests' or 'this appears consistent with'.",
        "This is proactive communication flow. Respond for the client as needed.",
        "Format the response in plain, easy-to-read text using short sections and concise bullets.",
        "Do not use markdown emphasis markers such as **, __, or * for styling.",
        "For research responses, always include sections in this order: Overview, Key points, What to do next, Live sources.",
        "In Live sources, output raw links only, one per line.",
        "signal_json:",
        json.dumps(signal_payload, default=str),
    ]
    if conversation_entries:
        lines.extend([
            "recent_conversation_context_json:",
            json.dumps(conversation_entries, default=str),
        ])
    if research_context:
        lines.extend([
            "research_context_json:",
            json.dumps(research_context, default=str),
        ])
    return "\n".join(lines)


def _safe_json_loads(raw: str) -> dict[str, Any]:
    """
    Parse JSON object string safely and return an empty dict on failure.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_signal_research_query(fields: dict[str, Any]) -> str:
    """
    Build a focused research query from a signal payload.
    """
    signal_type = str(fields.get("signal_type") or "").strip().lower()
    payload = _safe_json_loads(str(fields.get("payload") or "{}"))

    mode = str(payload.get("mode") or "").strip().lower()
    if not mode:
        mode = "product_review" if signal_type.startswith("product_review_") else "diagnose"

    if mode == "product_review":
        product_name = str(payload.get("product_name") or "").strip()
        description = str(payload.get("description") or "").strip()
        subject = product_name or description
        if not subject:
            return ""
        return (
            f"{subject} manufacturer and clinical-use summary, including indications, "
            "contraindications, pregnancy/breastfeeding safety, and practical usage cautions"
        )

    diagnosis = str(payload.get("diagnosis") or "").strip()
    diagnosis_text = str(payload.get("diagnosis_text") or "").strip()
    subject = diagnosis or diagnosis_text
    if not subject:
        return ""
    return (
        f"{subject} overview including likely causes, evidence-based management options, "
        "and urgent red-flag symptoms"
    )


def _perform_signal_research(fields: dict[str, Any]) -> dict[str, Any]:
    """
    Run search tools before composing a proactive signal message.
    """
    query = _extract_signal_research_query(fields)
    if not query:
        return {
            "status": "skipped",
            "reason": "no_research_query",
            "query": "",
            "internet_search": "",
            "wikipedia_search": "",
        }

    result: dict[str, Any] = {
        "status": "ok",
        "query": query,
        "internet_search": "",
        "wikipedia_search": "",
    }

    try:
        internet_raw = internet_search.invoke({"query": query, "max_results": 6})
        result["internet_search"] = str(internet_raw)
    except Exception as exc:
        result["status"] = "partial"
        result["internet_error"] = str(exc)

    try:
        wiki_raw = wikipedia_search.invoke({"query": query, "max_results": 3})
        result["wikipedia_search"] = str(wiki_raw)
    except Exception as exc:
        result["status"] = "partial"
        result["wikipedia_error"] = str(exc)

    return result


def _process_signal_message(agent: Any, msg_id: str, fields: dict[str, Any]) -> None:
    """
    Process an internal orchestrator signal message.
    The Signal stream is only updated - after case completion or case failure.
    Intermediary updates are discouraged, since the agent uses this stream to
    proactively reach out to the client.

    Isolation from the job flow:
        - Runs under ``checkpoint_ns="orchestrator:signal"`` so it does NOT
          mutate the user-facing conversation checkpoint.
        - On success, appends a compact entry to the per-client proactive log,
          which the job flow reads (read-only) on the next user turn so the
          orchestrator knows what was already proactively sent.

    Args:
        agent: The orchestrator agent instance
        msg_id: The message ID from the Redis stream
        fields: Dictionary containing signal fields

    Returns:
        None
    """
    client_id = str(fields.get("client_id") or "anonymous")
    case_id = str(fields.get("case_id") or "")
    channel = str(fields.get("channel") or "")
    request_id = str(fields.get("request_id") or "")
    signal_type = str(fields.get("signal_type") or "")
    source_message_id = str(fields.get("message_id") or "").strip()
    reply_to_message_id = (source_message_id if signal_type.startswith("diagnose_")
    or signal_type.startswith("product_review_") else "")

    logger.info("___-------------------_______")
    logger.info(f"THIS IS THE SIGNAL TYPE: {signal_type}")
    logger.info("___-------------------_______")
    try:
        payload_obj: dict[str, Any] = {}
        try:
            payload_obj = json.loads(str(fields.get("payload") or "{}"))
        except Exception:
            payload_obj = {}
        metadata_obj = payload_obj.get("metadata") or {}
        extras_obj = metadata_obj.get("extras") if isinstance(metadata_obj, dict) else {}
        extras_obj = extras_obj if isinstance(extras_obj, dict) else {}
        group_id = str(extras_obj.get("group_id") or "").strip()
        group_expected_count = int(str(extras_obj.get("group_expected_count") or "1"))
        signal_confidence = extract_confidence_from_fields(fields)
        signal_threshold = extract_threshold_from_fields(fields)

        # Mark signal-in-flight so a concurrent job flow knows to wait briefly
        # for this signal's proactive_log write before composing its reply.
        # Signals are higher priority and never wait on the job marker
        # (asymmetric rule) to avoid delaying time-sensitive diagnoses.
        _set_inflight(
            "signal",
            client_id,
            {
                "signal_type": signal_type,
                "signal_id": str(fields.get("signal_id") or ""),
                "request_id": request_id,
                "case_id": case_id,
                "ts_ms": int(time.time() * 1000),
            },
        )

        if signal_type.startswith("product_review_"):
            logger.info(
                "processing product-review signal through rules flow: request_id=%s case_id=%s client_id=%s signal_type=%s",
                request_id,
                case_id,
                client_id,
                signal_type,
            )
            rules_result = run_product_signal_rules_flow(
                fields=fields,
                context=_read_conversation_log(client_id, 10),
            )
            summary = str(rules_result.get("summary") or "").strip()
            friendly_report = str(rules_result.get("user_friendly_report") or "").strip()
            sources = rules_result.get("sources") or []
            sources_text = "\n".join(str(item) for item in sources if str(item).strip())
            primary_text = friendly_report or summary
            reply = primary_text if not sources_text else f"{primary_text}\n\nSources:\n{sources_text}"

        elif signal_type.startswith("diagnose_") or signal_type.startswith("skin_"):
            logger.info(
                "processing skin-review signal through rules flow: request_id=%s case_id=%s client_id=%s signal_type=%s",
                request_id,
                case_id,
                client_id,
                signal_type,
            )
            rules_result = run_skin_signal_rules_flow(
                fields=fields,
                context=_read_conversation_log(client_id, 10),
            )
            summary = str(rules_result.get("summary") or "").strip()
            friendly_report = str(rules_result.get("user_friendly_report") or "").strip()
            sources = rules_result.get("sources") or []
            sources_text = "\n".join(str(item) for item in sources if str(item).strip())
            primary_text = friendly_report or summary
            reply = primary_text if not sources_text else f"{primary_text}\n\nSources:\n{sources_text}"
        else:
            # Read recent conversations first and build an input for the signal
            conversation_entries = _read_conversation_log(client_id, CONVERSATION_LOG_INCLUDE_N)
            research_context = _perform_signal_research(fields)
            signal_prompt = _build_signal_input(
                fields,
                conversation_entries=conversation_entries,
                research_context=research_context,
            )
            config: RunnableConfig = build_invoke_config(
                thread_id=client_id,
                checkpoint_ns="orchestrator:signal",
                run_name=f"orchestrator:signal",
                channel=channel,
                flow="signal",
                client_id=client_id,
                request_id=request_id,
                case_id=case_id,
                message_id=str(fields.get("message_id") or request_id),
                recursion_limit=AGENT_RECURSION_LIMIT,
                extra_metadata={
                    "signal_type": str(fields.get("signal_type") or ""),
                    "signal_id": str(fields.get("signal_id") or ""),
                    **({"confidence": signal_confidence} if signal_confidence else {}),
                    **({"threshold": signal_threshold} if signal_threshold else {}),
                },
            )
            agent_call_tracer.start_turn(
                request_id=request_id,
                case_id=case_id,
                client_id=client_id,
                channel=channel,
                flow="signal",
            )
            res = agent.invoke(
                {"messages": [{"role": "user", "content": signal_prompt}]},
                config=config,
            )
            agent_call_tracer.set_tool_names(agent_call_tracer.extract_tool_names(res))
            agent_call_tracer.end_turn(status="ok")
            reply = (
                res.get("structured_response").response
                if isinstance(res, dict) and res.get("structured_response") is not None
                else str(res)
            )

        reply = _strip_markdown_formatting(reply)

        print(reply)
        logger.info("Orchestrator proactive reply: %s", reply)

        delivery_prompt = format_delivery_context_for_prompt(
            load_delivery_context(case_id, client_id, redis_client=_redis)
        )

        # If the signal is part of a group, aggregate the replies
        if group_id and group_expected_count > 1:
            logger.info("___-------------------_______")
            logger.info(f"THIS IS THE GROUP ID: {group_id}")
            logger.info(f"THIS IS THE GROUP EXPECTED COUNT: {group_expected_count}")
            logger.info("___-------------------_______")
            aggregate_result = collect_and_compile_if_ready(
                group_id=group_id,
                expected_count=group_expected_count,
                request_id=request_id,
                signal_type=signal_type,
                item_text=reply,
                item_payload={
                    "fields": fields,
                    "reply": reply,
                },
                delivery_context_prompt=delivery_prompt,
            )
            if not aggregate_result.should_send:
                _redis.xack(ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP, msg_id)
                _redis.xdel(ORCHESTRATOR_SIGNAL_STREAM, msg_id)
                reclaim_logic.mark_processed(ORCHESTRATOR_SIGNAL_STREAM, msg_id)
                return
            reply = aggregate_result.compiled_text

        _enqueue_outbound(
            client_id=client_id,
            case_id=case_id,
            channel=channel,
            request_id=request_id,
            text=reply,
            kind="signal_update",
            must_deliver=True,
            reply_to_message_id=reply_to_message_id,
            confidence=signal_confidence or None,
            threshold=signal_threshold or None,
        )

        _finalize_intake_after_delivery(case_id, client_id)

        # Persist a compact record of the proactive message so the next user
        # turn (job flow) can see what was already sent. Best-effort.
        _append_proactive_log(
            client_id,
            {
                "ts_ms": int(time.time() * 1000),
                "signal_type": str(fields.get("signal_type") or "unknown"),
                "signal_id": str(fields.get("signal_id") or ""),
                "request_id": request_id,
                "case_id": str(fields.get("case_id") or ""),
                "source_event_id": str(fields.get("source_event_id") or ""),
                "reply": reply,
            },
        )

        _redis.xack(ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_SIGNAL_STREAM, msg_id)

        # Mark as processed for idempotency
        reclaim_logic.mark_processed(ORCHESTRATOR_SIGNAL_STREAM, msg_id)

    except Exception as exc:
        # Finalize the trace for this turn (no-op if already ended on success).
        agent_call_tracer.end_turn(status="failed", error=str(exc))
        _redis.xadd(
            ORCHESTRATOR_SIGNAL_DLQ_STREAM,
            {
                "request_id": request_id,
                "client_id": client_id,
                "source_stream": ORCHESTRATOR_SIGNAL_STREAM,
                "error": str(exc),
                "payload": json.dumps(fields, default=str),
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
        _redis.xack(ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_SIGNAL_STREAM, msg_id)
        logger.exception("orchestrator signal failed: request_id=%s client_id=%s", request_id, client_id)

    finally:
        # Always clear the signal in-flight marker so any waiting job flow
        # unblocks promptly rather than waiting for TTL.
        _del_inflight("signal", client_id)


def run_worker_forever() -> None:
    """
    Run the orchestrator worker until interrupted.

    Architecture:
        - Two dedicated reader threads, one per stream (jobs, signals).
        - Each reader has its own capacity semaphore so neither stream can
          starve the other.
        - Both readers submit handler tasks to a single shared thread pool
          sized to the sum of both capacities.
        - A ``stop_event`` propagates shutdown (KeyboardInterrupt or reader
          thread death) to both readers cooperatively.
    """
    # Check REDIS connection, ensure consumer groups exist for both streams.
    check_redis_connectivity(decode_responses=True)
    _ensure_consumer_group("orchestrator_job")
    _ensure_consumer_group("signal_job")

    # Open Redis checkpointer once for worker lifetime, create required
    # checkpoint indices, then close cleanly on shutdown.
    checkpointer, checkpointer_ctx = open_checkpointer_url(REDIS_URL)

    # Expose the checkpointer for poisoned-thread resets on recursion failures.
    global _checkpointer
    _checkpointer = checkpointer

    log_tracing_status("orchestrator")
    log_pipeline_tracing_status("orchestrator")

    # Initialize the dynamic agent (shared by both readers; safe because the
    # agent is invoked via thread-safe checkpointer state per thread_id).
    agent = _build_dynamic_agent(checkpointer=checkpointer)

    logger.info(
        "orchestrator worker started: "
        "job_stream=%s job_group=%s job_consumer=%s job_max_inflight=%d job_batch=%d job_block_ms=%d "
        "signal_stream=%s signal_group=%s signal_consumer=%s signal_max_inflight=%d signal_batch=%d signal_block_ms=%d",
        ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, ORCHESTRATOR_CONSUMER_NAME,
        JOB_MAX_INFLIGHT, JOB_BATCH_MAX, JOB_BLOCK_MS,
        ORCHESTRATOR_SIGNAL_STREAM, ORCHESTRATOR_SIGNAL_CONSUMER_GROUP, ORCHESTRATOR_SIGNAL_CONSUMER_NAME,
        SIGNAL_MAX_INFLIGHT, SIGNAL_BATCH_MAX, SIGNAL_BLOCK_MS,
    )

    # Per-stream capacity. Total OS thread count = sum of both, so a saturated
    # job stream cannot consume signal slots and vice versa.
    job_capacity = threading.Semaphore(JOB_MAX_INFLIGHT)
    signal_capacity = threading.Semaphore(SIGNAL_MAX_INFLIGHT)
    total_workers = JOB_MAX_INFLIGHT + SIGNAL_MAX_INFLIGHT

    stop_event = threading.Event()

    with ThreadPoolExecutor(max_workers=total_workers, thread_name_prefix="orch-worker") as pool:
        job_thread = threading.Thread(
            target=_reader_loop,
            kwargs=dict(
                stream=ORCHESTRATOR_JOB_STREAM,
                group=ORCHESTRATOR_CONSUMER_GROUP,
                consumer=ORCHESTRATOR_CONSUMER_NAME,
                handler=_process_stream_message,
                agent=agent,
                pool=pool,
                capacity=job_capacity,
                batch_max=JOB_BATCH_MAX,
                block_ms=JOB_BLOCK_MS,
                stop_event=stop_event,
            ),
            name="orchestrator-job-reader",
            daemon=True,
        )
        signal_thread = threading.Thread(
            target=_reader_loop,
            kwargs=dict(
                stream=ORCHESTRATOR_SIGNAL_STREAM,
                group=ORCHESTRATOR_SIGNAL_CONSUMER_GROUP,
                consumer=ORCHESTRATOR_SIGNAL_CONSUMER_NAME,
                handler=_process_signal_message,
                agent=agent,
                pool=pool,
                capacity=signal_capacity,
                batch_max=SIGNAL_BATCH_MAX,
                block_ms=SIGNAL_BLOCK_MS,
                stop_event=stop_event,
            ),
            name="orchestrator-signal-reader",
            daemon=True,
        )

        # Start reclaim threads for both streams
        job_reclaim_thread = _start_reclaim_thread(
            stream=ORCHESTRATOR_JOB_STREAM,
            group=ORCHESTRATOR_CONSUMER_GROUP,
            consumer=ORCHESTRATOR_CONSUMER_NAME,
            handler=_process_stream_message,
            agent=agent,
            dlq_stream=ORCHESTRATOR_DLQ_STREAM,
            stop_event=stop_event,
        )
        signal_reclaim_thread = _start_reclaim_thread(
            stream=ORCHESTRATOR_SIGNAL_STREAM,
            group=ORCHESTRATOR_SIGNAL_CONSUMER_GROUP,
            consumer=ORCHESTRATOR_SIGNAL_CONSUMER_NAME,
            handler=_process_signal_message,
            agent=agent,
            dlq_stream=ORCHESTRATOR_SIGNAL_DLQ_STREAM,
            stop_event=stop_event,
        )

        job_thread.start()
        signal_thread.start()

        try:
            # Supervise: exit if any critical thread dies or on Ctrl+C.
            while (
                job_thread.is_alive()
                and signal_thread.is_alive()
                and job_reclaim_thread.is_alive()
                and signal_reclaim_thread.is_alive()
            ):
                stop_event.wait(timeout=1.0)
                if stop_event.is_set():
                    break
        except KeyboardInterrupt:
            logger.info("orchestrator worker received KeyboardInterrupt; shutting down")
        finally:
            stop_event.set()
            job_thread.join(timeout=5.0)
            signal_thread.join(timeout=5.0)
            job_reclaim_thread.join(timeout=5.0)
            signal_reclaim_thread.join(timeout=5.0)
            try:
                checkpointer_ctx.__exit__(None, None, None)
            except Exception:
                logger.exception("failed to close checkpointer context")
            logger.info("orchestrator worker stopped")


if __name__ == "__main__":
    run_worker_forever()


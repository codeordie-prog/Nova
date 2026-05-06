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
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from typing import List, Dict, Any, Callable, Optional
from pydantic import BaseModel
import redis


from dermascan.backend.services.tools.orchestrator.orchestrator_tools import orchestrator_tools
from dermascan.backend.services.agents.loader import load_agent_configurations
from dermascan.backend.services.agents.build import (
    open_checkpointer_url,
    build_google_credentials,
)
from dermascan.shared.logger import get_logger
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware, wrap_model_call, ModelRequest, ModelResponse
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from dermascan.backend.services.agents.prompts.orchestrator_prompt import system_prompt


# Load environment variables
load_dotenv()

# Get env variables
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY_QUTIS", "")
CRED_PATH: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_QUTIS", "")
PROJECT_ID: str = os.getenv("GOOGLE_CLOUD_PROJECT_QUTIS", "qutisai")
LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
REDIS_URL: str = os.getenv("REDIS_URL", "")
BASE_DIR: str = os.path.dirname(__file__)
CONFIG_PATH: str = os.path.join(BASE_DIR, "configurations", "agents_config.json")

# Define default configurations
DEFAULT_CONFIG: List[Dict[str, Any]] = [{
        "name": "orchestrator-google",
        "description": "Conversational Agent for orchestrating the diagnosis process",
        "model":"gemini-2.5-flash",
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
_redis: redis.Redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)


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
    _redis.xadd(
        OUTBOUND_STREAM,
        {
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
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
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


# Build the orchestrator input
# TO BE REVISED WITH RESPECT TO THE SCHEMA OF THE PAYLOAD SEND FROM WHATSAPP WEBHOOK ENDPOINT
# TODO: Add more fields to the input - This maybe change with respect to the SCHEMA of the payload
def _build_orchestrator_input(
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    username: Optional[str],
    payload_json: str,
) -> str:

    """
    Builds the input for the orchestrator agent.
    
    Args:
        request_id (str): Request ID
        case_id (str): Case ID
        client_id (str): Client ID
        message_id (str): Message ID
        channel (str): Channel
        username (Optional[str]): Username
        payload_json (str): JSON string containing the payload
        
    Returns:
        str: The input for the orchestrator agent
    """
    try:
        # Parse the payload
        payload: dict[str, Any] = json.loads(payload_json)
    except Exception:
        payload = {}

    # Extract content
    content = payload.get("content") or {}

    # Extract text and attachments
    text: str = str(content.get("text") or "").strip()
    attachments: list = content.get("attachments") or []

    # Build summary lines
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
        "full_payload_json:",
        json.dumps(payload, default=str),
    ]

    # Add routing hints
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

    # Inject the recent proactive-message log so the orchestrator is aware of
    # what the system has already proactively sent to this client. This is a
    # read-only snapshot from the signal flow; the job flow never writes here.
    proactive_entries = _read_proactive_log(client_id, PROACTIVE_LOG_INCLUDE_N)
    if proactive_entries:
        summary_lines.append("proactive_messages_recently_sent_to_client:")
        summary_lines.append(json.dumps(proactive_entries, default=str))
        summary_lines.append(
            "Context hint: do not repeat or contradict the proactive messages above."
        )

    return "\n".join(summary_lines)

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
    Build a dynamic agent with Google and OpenAI providers.
    This dynamic Logic is important for creating fallbacks.
    
    Returns:
        Any: A dynamic agent with Google and OpenAI providers
    """
    # Get config files
    google_cfg = get_config_file(CONFIG_PATH, "google")
    openai_cfg = get_config_file(CONFIG_PATH, "openai")

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required for fallback provider")

    # Get Google service account access credentials
    creds = build_google_credentials(path=CRED_PATH)

    # Define Primary model
    primary_model = ChatGoogleGenerativeAI(
        model=google_cfg["model"],
        credentials=creds,
        temperature=google_cfg["temperature"],
        max_tokens=google_cfg["max_tokens"],
        top_p=google_cfg["top_p"],
        location=LOCATION,
        project=PROJECT_ID,
        api_key=GEMINI_API_KEY,
    )
    
    # Define Fallback model
    fallback_model = ChatOpenAI(
        model=openai_cfg["model"],
        temperature=openai_cfg["temperature"],
        max_tokens=openai_cfg["max_tokens"],
        api_key=OPENAI_API_KEY,
    )

    # Define dynamic model selection MIDDLEWARE
    @wrap_model_call
    def dynamic_model_selection(request: ModelRequest, handler) -> ModelResponse:
        """
        Dynamic model selection middleware that tries primary model first,
        and falls back to fallback model if primary model is exhausted.
        This Logic tries to address the common 429 error (rate limit exceeded)
        by switching to a different provider when the current one is overloaded.
        
        Args:
            request: The model request
            handler: The model handler
            
        Returns:
            ModelResponse: The model response
        """
        primary_request = request.override(model=primary_model)
        try:
            return handler(primary_request)

        except Exception as exc:
            if not _is_resource_exhaustion_error(exc):
                raise
            logger.warning("Primary model exhausted; retrying with fallback model")
            fallback_request = request.override(model=fallback_model)
            return handler(fallback_request)

    soul_str = ""

    # Load Soul.md
    soul_path = os.path.join(BASE_DIR, "soul.md")
    try:
        with open(soul_path, "r", encoding="utf-8") as soul_file:
            soul_content = soul_file.read()
            if soul_content:
                soul_str = f"Here is your core values and origin provided in this soul description: \n\n{soul_content}"
    except Exception:
        logger.warning("failed to load soul.md from %s; continuing with base system prompt", soul_path)
    

    return create_agent(
        model=primary_model,
        tools=orchestrator_tools,
        system_prompt=soul_str + system_prompt,
        middleware=[
            dynamic_model_selection,
            SummarizationMiddleware(
                model=primary_model,
                trigger=("tokens", 1000),
                keep=("messages", 10),
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
            resp = _redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=slots_held,
                block=block_ms,
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
                    handler(agent, _mid, _fields)
                except Exception:
                    # Handlers are expected to handle their own errors (DLQ + ack).
                    # This is a last-resort guard so a bug cannot leak a slot.
                    logger.exception(
                        "unexpected handler failure: stream=%s msg_id=%s", stream, _mid
                    )
                finally:
                    capacity.release()

            pool.submit(_run)


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

        # Mark our own flow as in-flight so a concurrently arriving signal can
        # decide (today: does not wait; future: may read for context).
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

        user_prompt = _build_orchestrator_input(
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            message_id=message_id,
            channel=channel,
            username=username,
            payload_json=payload_json,
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": client_id,
                "checkpoint_ns": "orchestrator",
            }
        }
        res = agent.invoke(
            {"messages": [{"role": "user", "content": user_prompt}]},
            config=config,
        )
        reply = (
            res.get("structured_response").response
            if isinstance(res, dict) and res.get("structured_response") is not None
            else str(res)
        )

        print(reply)
        logger.info("Orchestrator reply: %s", reply)

        try:
            payload_obj: dict[str, Any] = json.loads(payload_json)
        except Exception:
            payload_obj = {}
        user_text = str(((payload_obj.get("content") or {}).get("text") or "")).strip()

        # Append to conversational Log
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

        # Add to outbound stream
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

        # Acknowledge the job stream message and delete it from the stream
        _redis.xack(ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_JOB_STREAM, msg_id)

    except Exception as exc:

        # Add the failed job to the dead letter queue
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
        # Acknowledge the job stream message and delete it from the job stream
        _redis.xack(ORCHESTRATOR_JOB_STREAM, ORCHESTRATOR_CONSUMER_GROUP, msg_id)
        _redis.xdel(ORCHESTRATOR_JOB_STREAM, msg_id)
        logger.exception("orchestrator job failed: request_id=%s client_id=%s", request_id, client_id)

    finally:
        # Always clear the job in-flight marker so a queued peer flow can proceed
        # without waiting for the TTL to expire.
        _del_inflight("job", client_id)


def _build_signal_input(
    fields: dict[str, Any],
    conversation_entries: Optional[List[dict[str, Any]]] = None,
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
        "This is proactive communication flow. Respond for the client as needed.",
        "signal_json:",
        json.dumps(signal_payload, default=str),
    ]
    if conversation_entries:
        lines.extend([
            "recent_conversation_context_json:",
            json.dumps(conversation_entries, default=str),
        ])
    return "\n".join(lines)


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
    reply_to_message_id = source_message_id if signal_type.startswith("diagnose_") else ""

    try:
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

        # Read recent conversations first and build an input for the signal
        conversation_entries = _read_conversation_log(client_id, CONVERSATION_LOG_INCLUDE_N)
        signal_prompt = _build_signal_input(
            fields,
            conversation_entries=conversation_entries,
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": client_id,
                # Separate namespace prevents races with concurrent job-flow
                # invokes on the same client_id (jobs use ns="orchestrator").
                "checkpoint_ns": "orchestrator:signal",
            }
        }
        res = agent.invoke(
            {"messages": [{"role": "user", "content": signal_prompt}]},
            config=config,
        )
        reply = (
            res.get("structured_response").response
            if isinstance(res, dict) and res.get("structured_response") is not None
            else str(res)
        )

        print(reply)
        logger.info("Orchestrator proactive reply: %s", reply)

        _enqueue_outbound(
            client_id=client_id,
            case_id=case_id,
            channel=channel,
            request_id=request_id,
            text=reply,
            kind="signal_update",
            must_deliver=True,
            reply_to_message_id=reply_to_message_id,
        )

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
    except Exception as exc:
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
    _redis.ping()
    _ensure_consumer_group("orchestrator_job")
    _ensure_consumer_group("signal_job")

    # Open Redis checkpointer once for worker lifetime, create required
    # checkpoint indices, then close cleanly on shutdown.
    checkpointer, checkpointer_ctx = open_checkpointer_url(REDIS_URL)

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

        job_thread.start()
        signal_thread.start()

        try:
            # Supervise: exit if any critical thread dies or on Ctrl+C.
            while (
                job_thread.is_alive()
                and signal_thread.is_alive()
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
            try:
                checkpointer_ctx.__exit__(None, None, None)
            except Exception:
                logger.exception("failed to close checkpointer context")
            logger.info("orchestrator worker stopped")


if __name__ == "__main__":
    run_worker_forever()


# TODO
# 1. Thread locking to preserve causal flow of events
# 2. System prompt + context engineering
# 3. Dynamic tooling
# 4. Dynamic System prompting
# 5. Direct Soul.md ownership - Qutis should update its own soul NOT US. 
# 5. Direct Soul.md ownership - Qutis should update its own soul NOT US. 
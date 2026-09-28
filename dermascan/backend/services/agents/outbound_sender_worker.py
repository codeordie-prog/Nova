"""
Author: Dermascan Team
Date: 2026-09-24
License: MIT

Ordered outbound sender worker.
"""

import json
import logging
import os
import re
import socket
import threading
import zlib
from collections import defaultdict
from typing import Any

import redis
from dermascan.shared.load_env import ensure_env_loaded

from dermascan.shared.logger import get_logger
from dermascan.shared.stream_pipeline_tracing import (
    log_pipeline_tracing_status,
    run_traced_stream_handler,
)
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)
from dermascan.backend.services.agents import reclaim_logic
from dermascan.backend.utils.backoff import post_with_outbound_retry
from dermascan.shared.whatsapp_platform_message_id import is_whatsapp_platform_message_id

ensure_env_loaded()

REDIS_URL: str = os.getenv("REDIS_URL", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_HTTP_TIMEOUT_S: float = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "20"))
TELEGRAM_TLS_VERIFY: str = os.getenv("TELEGRAM_TLS_VERIFY", "false")
TELEGRAM_CA_BUNDLE: str = os.getenv("TELEGRAM_CA_BUNDLE", "")
WHATSAPP_API_BASE_URL: str = os.getenv("WHATSAPP_API_BASE_URL", "http://127.0.0.1:8000")
WHATSAPP_API_SEND_PATH: str = os.getenv("WHATSAPP_API_SEND_PATH", "/send")
WHATSAPP_API_TYPING_PATH: str = os.getenv("WHATSAPP_API_TYPING_PATH", "/typing")
WHATSAPP_HTTP_TIMEOUT_S: float = float(os.getenv("WHATSAPP_HTTP_TIMEOUT_S", "20"))
WHATSAPP_TLS_VERIFY: str = os.getenv("WHATSAPP_TLS_VERIFY", "true")
WHATSAPP_CA_BUNDLE: str = os.getenv("WHATSAPP_CA_BUNDLE", "")
WHATSAPP_INTERNAL_API_SECRET: str = os.getenv(
    "WHATSAPP_INTERNAL_API_SECRET",
    os.getenv("INTERNAL_API_SECRET", ""),
)

OUTBOUND_STREAM: str = os.getenv("OUTBOUND_STREAM", "outbound:messages")
OUTBOUND_CONSUMER_GROUP: str = os.getenv("OUTBOUND_CONSUMER_GROUP", "outbound-sender-workers")
OUTBOUND_CONSUMER_NAME_BASE: str = os.getenv("OUTBOUND_CONSUMER_NAME", "outbound-sender-worker")
OUTBOUND_CONSUMER_NAME: str = f"{OUTBOUND_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
OUTBOUND_DLQ_STREAM: str = os.getenv("OUTBOUND_DLQ_STREAM", "outbound:dlq")
OUTBOUND_BATCH_MAX: int = max(1, int(os.getenv("OUTBOUND_BATCH_MAX", "64")))
OUTBOUND_BLOCK_MS: int = int(os.getenv("OUTBOUND_BLOCK_MS", "1000"))
OUTBOUND_DEDUPE_TTL_SEC: int = max(30, int(os.getenv("OUTBOUND_DEDUPE_TTL_SEC", "600")))
OUTBOUND_CONVO_FIRST_WINDOW_MS: int = max(0, int(os.getenv("OUTBOUND_CONVO_FIRST_WINDOW_MS", "1500")))
OUTBOUND_CHAOS_WINDOW_MS: int = max(0, int(os.getenv("OUTBOUND_CHAOS_WINDOW_MS", "2000")))
OUTBOUND_CHAOS_TTL_SEC: int = max(1, int(os.getenv("OUTBOUND_CHAOS_TTL_SEC", "20")))
OUTBOUND_SEND_MAX_RETRIES: int = max(0, int(os.getenv("OUTBOUND_SEND_MAX_RETRIES", "3")))
OUTBOUND_SEND_RETRY_BASE_DELAY_S: float = max(
    0.05,
    float(os.getenv("OUTBOUND_SEND_RETRY_BASE_DELAY_S", "0.5")),
)
OUTBOUND_TYPING_ENABLED: bool = os.getenv("OUTBOUND_TYPING_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

logger: logging.Logger = get_logger("agents")
_redis: redis.Redis = get_redis_client(decode_responses=True)

# Initialize reclaim_logic with the Redis client
reclaim_logic.set_redis_client(_redis)


def _ensure_consumer_group() -> None:
    """
    Ensure consumer group exists.
    """
    try:
        _redis.xgroup_create(OUTBOUND_STREAM, OUTBOUND_CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _split_telegram_text(text: str, max_len: int = 4096) -> list[str]:
    """
    Split text into chunks suitable for Telegram sendMessage.
    
    Telegram has a 4096 character limit per message.

    Args:
        text: The text to split.
        max_len: The maximum length of each chunk.
        
    Returns:
        A list of text chunks.
    """
    s = text.strip()
    if not s:
        return []
    return [s[i:i + max_len] for i in range(0, len(s), max_len)]


def _telegram_tls_verify() -> bool | str:
    """Return TLS verify setting for Telegram HTTP calls."""
    if TELEGRAM_CA_BUNDLE.strip():
        return TELEGRAM_CA_BUNDLE.strip()
    if TELEGRAM_TLS_VERIFY.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return True


def _whatsapp_tls_verify() -> bool | str:
    """Return TLS verify setting for WhatsApp sidecar HTTP calls."""
    if WHATSAPP_CA_BUNDLE.strip():
        return WHATSAPP_CA_BUNDLE.strip()
    if WHATSAPP_TLS_VERIFY.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return True


def _whatsapp_internal_headers() -> dict[str, str]:
    """Build auth headers for qutis sidecar internal endpoints."""
    return {
        "Content-Type": "application/json",
        "X-Internal-Secret": WHATSAPP_INTERNAL_API_SECRET,
    }


def _send_typing_best_effort(fields: dict[str, Any]) -> None:
    """
    Best-effort typing indicator before outbound delivery.

    Typing failures are logged and ignored so they never block delivery.

    Args:
        fields: Outbound stream fields.
    """
    if not OUTBOUND_TYPING_ENABLED:
        return

    channel = str(fields.get("channel") or "")
    case_id = str(fields.get("case_id") or "").strip()
    reply_ref = str(fields.get("reply_to_message_id") or "").strip()

    try:
        if channel == "telegram":
            if not case_id or not TELEGRAM_BOT_TOKEN:
                return
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
            post_with_outbound_retry(
                url=url,
                max_retries=0,
                base_delay_s=OUTBOUND_SEND_RETRY_BASE_DELAY_S,
                json={"chat_id": case_id, "action": "typing"},
                timeout=min(TELEGRAM_HTTP_TIMEOUT_S, 5.0),
                verify=_telegram_tls_verify(),
            )
            return

        if channel == "whatsapp":
            if (
                not reply_ref
                or not WHATSAPP_INTERNAL_API_SECRET
                or not is_whatsapp_platform_message_id(reply_ref)
            ):
                return
            base_url = WHATSAPP_API_BASE_URL.rstrip("/")
            typing_path = (
                WHATSAPP_API_TYPING_PATH
                if WHATSAPP_API_TYPING_PATH.startswith("/")
                else f"/{WHATSAPP_API_TYPING_PATH}"
            )
            url = f"{base_url}{typing_path}"
            post_with_outbound_retry(
                url=url,
                max_retries=0,
                base_delay_s=OUTBOUND_SEND_RETRY_BASE_DELAY_S,
                json={"message_id": reply_ref},
                headers=_whatsapp_internal_headers(),
                timeout=min(WHATSAPP_HTTP_TIMEOUT_S, 5.0),
                verify=_whatsapp_tls_verify(),
            )
    except Exception:
        logger.debug(
            "typing indicator failed: channel=%s case_id=%s reply_ref=%s",
            channel,
            case_id,
            reply_ref,
            exc_info=True,
        )


def _post_telegram_chunks(
    chat_id: str,
    text: str,
    *,
    reply_to_message_id: int | None,
) -> None:
    """
    Post one Telegram message, splitting into API-sized chunks when needed.

    Args:
        chat_id: Telegram chat id.
        text: Message body.
        reply_to_message_id: Optional message id to thread under.

    Raises:
        RuntimeError: When the Telegram API returns a non-ok response.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    verify = _telegram_tls_verify()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in _split_telegram_text(text):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True

        resp = post_with_outbound_retry(
            url=url,
            max_retries=OUTBOUND_SEND_MAX_RETRIES,
            base_delay_s=OUTBOUND_SEND_RETRY_BASE_DELAY_S,
            json=payload,
            timeout=TELEGRAM_HTTP_TIMEOUT_S,
            verify=verify,
        )
        response_payload = resp.json() if resp.content else {}
        if not response_payload.get("ok"):
            raise RuntimeError(f"telegram send non-ok: {response_payload}")


def _send_telegram(chat_id: str, text: str, reply_to_message_id: int | None = None) -> None:
    """
    Send a message to a Telegram chat.

    When threaded delivery fails, retries once without ``reply_to_message_id``
    so proactive signal replies still reach the client.

    Args:
        chat_id: The chat ID to send the message to.
        text: The text to send.
        reply_to_message_id: The message ID to reply to.
    """
    try:
        _post_telegram_chunks(chat_id, text, reply_to_message_id=reply_to_message_id)
    except RuntimeError as exc:
        if reply_to_message_id is None:
            raise
        logger.warning(
            "telegram threaded send failed; retrying without reply_to_message_id: chat_id=%s error=%s",
            chat_id,
            exc,
        )
        _post_telegram_chunks(chat_id, text, reply_to_message_id=None)


def _normalize_whatsapp_recipient(case_id: str) -> str:
    """
    Normalize recipient into E.164 digits expected by qutis /send.

    Args:
        case_id: Outbound case identifier from stream.

    Returns:
        Digits-only phone number without the leading plus sign.
    """
    normalized = str(case_id or "").strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    return normalized


def _send_whatsapp(case_id: str, text: str, reply_to_wamid: str | None = None) -> None:
    """
    Send a text message to WhatsApp through qutis sidecar /send endpoint.

    Args:
        case_id: Recipient identity (phone number).
        text: Message text body.
        reply_to_wamid: Optional WhatsApp message id for threading.
    """
    if not WHATSAPP_INTERNAL_API_SECRET:
        raise RuntimeError("WHATSAPP_INTERNAL_API_SECRET (or INTERNAL_API_SECRET) is missing")

    to = _normalize_whatsapp_recipient(case_id)
    if not to:
        raise ValueError("missing case_id for whatsapp outbound")

    verify = _whatsapp_tls_verify()

    base_url = WHATSAPP_API_BASE_URL.rstrip("/")
    send_path = WHATSAPP_API_SEND_PATH if WHATSAPP_API_SEND_PATH.startswith("/") else f"/{WHATSAPP_API_SEND_PATH}"
    url = f"{base_url}{send_path}"
    payload: dict[str, Any] = {"to": to, "kind": "text", "text": text}
    reply_target = str(reply_to_wamid or "").strip()
    if reply_target:
        payload["reply_to_wamid"] = reply_target
        logger.debug("whatsapp send threaded reply_to_wamid=%s", reply_target[:48])

    response = post_with_outbound_retry(
        url=url,
        max_retries=OUTBOUND_SEND_MAX_RETRIES,
        base_delay_s=OUTBOUND_SEND_RETRY_BASE_DELAY_S,
        json=payload,
        headers=_whatsapp_internal_headers(),
        timeout=WHATSAPP_HTTP_TIMEOUT_S,
        verify=verify,
    )
    response.raise_for_status()
    response_payload = response.json() if response.content else {}
    status_value = str(response_payload.get("status") or "").strip().lower()
    if status_value != "sent":
        raise RuntimeError(f"whatsapp send non-sent response: {response_payload}")


def _to_int(value: Any) -> int | None:
    """
    Convert a value to an integer.
    
    Args:
        value: The value to convert.
        
    Returns:
        The integer value, or None if the value is not a valid integer.
    """
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def _whatsapp_reply_wamid(fields: dict[str, Any]) -> str | None:
    """
    Resolve WhatsApp threading target (quoted reply to inbound message).

    Job replies after a user photo and proactive ``signal_update`` deliveries
    both carry the inbound ``message_id`` (wamid) in ``reply_to_message_id``.

    Args:
        fields: Outbound stream fields.

    Returns:
        Inbound wamid when this outbound should thread on WhatsApp, else None.
    """
    kind = _kind(fields)
    if kind not in {"signal_update", "job_reply"}:
        return None
    wamid = str(fields.get("reply_to_message_id") or "").strip()
    if not is_whatsapp_platform_message_id(wamid):
        return None
    return wamid


def _deliver(fields: dict[str, Any], reply_to_message_id: int | None = None) -> None:
    """
    Deliver a message to a channel.

    Args:
        fields: The fields to deliver.
        reply_to_message_id: Telegram reply target when threading is enabled.
    """
    channel = str(fields.get("channel") or "")
    # Recipient is the conversation/channel identity (phone for WhatsApp, chat for
    # Telegram), carried as client_id. case_id is the case identifier (a request_id)
    # and must never be used as a delivery address.
    recipient = str(fields.get("client_id") or "")
    text = str(fields.get("text") or "").strip()
    if not text:
        return

    _send_typing_best_effort(fields)

    if channel == "telegram":
        if not recipient:
            raise ValueError("missing client_id for telegram outbound")
        _send_telegram(recipient, text, reply_to_message_id=reply_to_message_id)
        return

    if channel == "whatsapp":
        _send_whatsapp(recipient, text, reply_to_wamid=_whatsapp_reply_wamid(fields))
        return
    raise ValueError(f"unsupported channel: {channel}")


# Acknowledge message from stream
def _ack_delete(msg_id: str) -> None:
    """
    Acknowledge and delete a message from the outbound stream.
    
    Args:
        msg_id: The ID of the message to acknowledge and delete.
    """
    _redis.xack(OUTBOUND_STREAM, OUTBOUND_CONSUMER_GROUP, msg_id)
    _redis.xdel(OUTBOUND_STREAM, msg_id)


def _dlq(msg_id: str, fields: dict[str, Any], error: str) -> None:
    """
    Add a message to the dead letter queue.
    
    Args:
        msg_id: The ID of the message to add to the dead letter queue.
        fields: The fields of the message.
        error: The error that occurred.
    """
    _redis.xadd(
        OUTBOUND_DLQ_STREAM,
        {
            "source_stream": OUTBOUND_STREAM,
            "source_msg_id": msg_id,
            "client_id": str(fields.get("client_id") or ""),
            "request_id": str(fields.get("request_id") or ""),
            "error": error,
            "payload": json.dumps(fields, default=str),
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )


def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, int]:
    """
    Sort key for outbound messages.
    
    Args:
        item: The item to sort.
        
    Returns:
        A tuple of (priority, created_at_ms, seq).
    """
    _, f = item
    return (int(f.get("priority") or 999), int(f.get("created_at_ms") or 0), int(f.get("seq") or 0))


def _int_field(fields: dict[str, Any], key: str, default: int = 0) -> int:
    """Read an integer field with a safe fallback default."""
    try:
        return int(fields.get(key) or default)
    except Exception:
        return default


def _kind(fields: dict[str, Any]) -> str:
    """
    Get the kind of the message.
    Kind is either a Job reply or Signal update
    
    Args:
        fields: The fields of the message.
        
    Returns:
        The kind of the message.
    """

    # Extract the 'kind' key from the fields dictionary
    # Kind key is added during outbound enquing in process stream / signal message functions

    return str(fields.get("kind") or "").strip().lower()


def _is_signal_kind(fields: dict[str, Any]) -> bool:
    """
    Check if the message is a signal update.
    
    Args:
        fields: The fields of the message.
        
    Returns:
        True if the message is a signal update, False otherwise.
    """
    return _kind(fields) == "signal_update"


def _is_job_kind(fields: dict[str, Any]) -> bool:
    """
    Check if the message is a job reply.

    Args:
        fields: The fields of the message.
        
    Returns:
        True if the message is a job reply, False otherwise.
    """
    return _kind(fields) == "job_reply"


def _reply_target(fields: dict[str, Any]) -> int | None:
    """
    Get the reply target of the message.
    The reply target is always the message ID.
    
    Args:
        fields: The fields of the message.
        
    Returns:
        The reply target of the message.
    """
    raw = str(fields.get("reply_to_message_id") or "").strip()
    if not raw:
        return None
    as_int = _to_int(raw)
    if as_int is not None:
        return as_int
    if is_whatsapp_platform_message_id(raw):
        return zlib.crc32(raw.encode("utf-8")) & 0x7FFFFFFF
    return None


def _inbound_ts_ms(fields: dict[str, Any]) -> int:
    """
    Get the inbound timestamp of the message.
    The inbound timestamp is the timestamp when the message was received.
    
    Args:
        fields: The fields of the message.
        
    Returns:
        The inbound timestamp of the message.
    """
    # Prefer explicit inbound timestamp when available; fallback to created_at.
    return _int_field(fields, "inbound_ts_ms", _int_field(fields, "created_at_ms", 0))


def _threading_decision(fields: dict[str, Any], chaos_mode: bool) -> tuple[bool, int | None]:
    """
    Determine if the message should be threaded and the reply target.
    
    Args:
        fields: The fields of the message.
        chaos_mode: Whether chaos mode is enabled.
        
    Returns:
        A tuple of (should_thread, reply_target).
    """
    target = _reply_target(fields)
    if _is_signal_kind(fields):
        return (target is not None, target)

    # If its a job reply and chaos mode is detected, thread it
    if _is_job_kind(fields) and chaos_mode:
        return (target is not None, target)
    
    # Otherwise, don't thread
    return (False, None)


def _chaos_state_key(client_id: str) -> str:
    return f"outbound:chaos:{client_id}"


def _detect_chaos_burst(items: list[tuple[str, dict[str, Any]]]) -> bool:
    """
    Detect bursty multi-turn user input for a client batch.

    We classify burst chaos when at least two distinct conversation turns
    (distinguished by reply target message id) appear within
    ``OUTBOUND_CHAOS_WINDOW_MS``.

    Args:
        items: The items to check.
        In the forever loop, each client messages are grouped together.
        Items is the selected(compact) list of messages for a client.
        
    Returns:
        True if chaos mode is detected, False otherwise.
    """
    # If CHAOS WINDOW IS SET TO 0 return False
    if OUTBOUND_CHAOS_WINDOW_MS <= 0:
        return False

    # Create a dictionary to store the earliest timestamp for each turn
    by_turn_earliest_ts: dict[int, int] = {}

    for _, fields in items:

        # Skip all signals
        if not _is_job_kind(fields):
            continue

        # Extract target message id
        turn_target = _reply_target(fields)
        if turn_target is None:
            continue
        
        # Get inbound timestamp
        ts = _inbound_ts_ms(fields)
        prev = by_turn_earliest_ts.get(turn_target)
        if prev is None or ts < prev:
            by_turn_earliest_ts[turn_target] = ts

    # Return false if length of dictionary is less than 2 - there is no chaos in 1 or less messages
    if len(by_turn_earliest_ts) < 2:
        return False

    # Sort by turns
    ordered_ts = sorted(by_turn_earliest_ts.values())
    for i in range(1, len(ordered_ts)):

        # Check if the difference between the current turn and the previous turn is less than the chaos window
        if ordered_ts[i] - ordered_ts[i - 1] <= OUTBOUND_CHAOS_WINDOW_MS:
            return True
    return False


def _detect_chaos_mode(client_id: str, items: list[tuple[str, dict[str, Any]]]) -> bool:
    """
    Determine chaos mode for a client using batch burst detection plus
    short-lived Redis state for continuity across adjacent read batches.

    Args:
        client_id: The client ID.
        items: The items to check.
        
    Returns:
        True if chaos mode is detected, False otherwise.
    """
    key = _chaos_state_key(client_id)
    burst = _detect_chaos_burst(items)

    # If burst is detected, set CHAOS key to True
    if burst:
        try:
            _redis.set(key, "1", ex=OUTBOUND_CHAOS_TTL_SEC)
        except Exception:
            logger.exception("failed to set chaos key: client_id=%s", client_id)
        return True

    try:
        return bool(_redis.get(key))
    except Exception:
        logger.exception("failed to read chaos key: client_id=%s", client_id)
        return False


def _delivery_order_key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, int, int, int]:
    """
    Final outbound ordering key.

    Order by inbound turn chronology first, then stabilize within a turn by
    kind (job reply before signal update), then by enqueue chronology.

    Args:
        item: The item to order.
        
    Returns:
        A tuple of (inbound_ts, turn_key, kind_rank, created_at_ms, seq).
    """
    _, fields = item
    # Get the inbound timestamp
    inbound_ts = _inbound_ts_ms(fields)
    # Get the reply target
    target = _reply_target(fields)
    # Set the turn key
    turn_key = target if target is not None else 2_147_483_647

    # Set the kind rank
    kind_rank = 9
    if _is_job_kind(fields):
        kind_rank = 1
    elif _is_signal_kind(fields):
        kind_rank = 2

    created_at_ms = _int_field(fields, "created_at_ms", 0)
    seq = _int_field(fields, "seq", 0)
    return (inbound_ts, turn_key, kind_rank, created_at_ms, seq)

def _normalized_text(value: str) -> str:
    s = value.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def _is_progress_text(text: str) -> bool:
    s = _normalized_text(text)
    progress_tokens = (
        "received",
        "processing",
        "analyzing",
        "analysing",
        "please wait",
        "bear with me",
        "working on",
        "in progress",
    )
    return any(token in s for token in progress_tokens)


####TOBE REVIEWED
def _is_recent_signal_conflict(job_fields: dict[str, Any], signal_ts_ms: list[int]) -> bool:
    """
    Determine whether a progress-style job reply is likely redundant because a
    signal update for the same case was emitted very recently.

    This handles cross-turn collisions where the signal and job reply do not
    share the same reply target message id.
    """
    if not signal_ts_ms:
        return False

    latest_signal_ts = max(signal_ts_ms)
    job_ts = _inbound_ts_ms(job_fields)
    if job_ts <= 0:
        return True

    # Use the larger of the two existing timing windows as the conservative
    # suppression horizon for near-concurrent signal/job collisions.
    window_ms = max(OUTBOUND_CHAOS_WINDOW_MS, OUTBOUND_CONVO_FIRST_WINDOW_MS)
    if window_ms <= 0:
        return job_ts >= latest_signal_ts

    if job_ts < latest_signal_ts:
        return False
    return (job_ts - latest_signal_ts) <= window_ms


def _compact_case_items(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """
    Compact one case's outbound items deterministically.

    Rules:
    - De-duplicate by (turn_target, kind, normalized_text).
    - Keep non-progress messages immediately.
    - Keep at most one progress job reply per turn.
    - Drop progress job replies for turns that already have a signal.
    """
    # Order first by turn chronology, then kind rank, then enqueue chronology.
    ordered = sorted(items, key=_delivery_order_key)

    # Track whether a signal exists for each turn target so we can suppress
    # progress-only job replies for that specific turn.
    # This is used later to suppress redundant progress replies for that same turn only.
    has_signal_by_turn: dict[int | None, bool] = {}
    signal_ts_ms: list[int] = []

    for _, fields in ordered:
        if _is_signal_kind(fields):
            has_signal_by_turn[_reply_target(fields)] = True
            signal_ts_ms.append(_inbound_ts_ms(fields))

    kept: list[tuple[str, dict[str, Any]]] = []
    # Dedupe per turn + kind + normalized text so independent turns do not
    # accidentally suppress each other.
    seen_turn_kind_text: set[tuple[int | None, str, str]] = set()
    latest_progress_job_by_turn: dict[int | None, tuple[str, dict[str, Any]]] = {}

    for mid, fields in ordered:
        kind = _kind(fields)
        turn_target = _reply_target(fields)
        text = str(fields.get("text") or "")
        text_key = _normalized_text(text)

        dedupe_key = (turn_target, kind, text_key)

        # Already emitted this exact turn/kind/text combination.
        if text_key and dedupe_key in seen_turn_kind_text:
            continue
        
        # Hold progress job replies; decide later whether to keep one per turn.
        if _is_job_kind(fields) and _is_progress_text(text):
            latest_progress_job_by_turn[turn_target] = (mid, fields)
            continue
        
        kept.append((mid, fields))
        if text_key:
            seen_turn_kind_text.add(dedupe_key)

    # Keep at most one progress job reply per turn, and suppress when a signal
    # exists in that same turn.
    for turn_target, progress_item in latest_progress_job_by_turn.items():
        if has_signal_by_turn.get(turn_target, False):
            continue
        mid, fields = progress_item
        if _is_recent_signal_conflict(fields, signal_ts_ms):
            continue
        text_key = _normalized_text(str(fields.get("text") or ""))
        dedupe_key = (turn_target, _kind(fields), text_key)
        if not text_key or dedupe_key not in seen_turn_kind_text:
            kept.append((mid, fields))
            if text_key:
                seen_turn_kind_text.add(dedupe_key)

    return sorted(kept, key=_delivery_order_key)

def _start_reclaim_thread(
    stop_event: threading.Event,
) -> threading.Thread:
    """
    Start a background thread that periodically reclaims stale pending messages.

    Args:
        stop_event: Cooperative shutdown flag.

    Returns:
        The started reclaim thread.
    """
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:
                def _reclaim_handler(msg_id: str, fields: dict[str, Any]) -> None:
                    # Mark as processed before handling to ensure idempotency
                    if reclaim_logic.mark_processed(OUTBOUND_STREAM, msg_id):
                        try:
                            # Re-process the message by adding it back to the stream
                            # This is a simplified approach - the message will be re-read normally
                            _redis.xadd(OUTBOUND_STREAM, fields)
                            # Ack and delete the reclaimed message
                            _redis.xack(OUTBOUND_STREAM, OUTBOUND_CONSUMER_GROUP, msg_id)
                            _redis.xdel(OUTBOUND_STREAM, msg_id)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                OUTBOUND_STREAM, msg_id,
                            )
                            # Mark as not processed so it can be retried
                            _redis.delete(reclaim_logic.processed_key(OUTBOUND_STREAM, msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=OUTBOUND_STREAM,
                    group=OUTBOUND_CONSUMER_GROUP,
                    consumer=OUTBOUND_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=OUTBOUND_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        OUTBOUND_STREAM, reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", OUTBOUND_STREAM)

            # Wait for the interval or stop event
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"outbound-reclaim-{OUTBOUND_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


def _compact_client_items(items: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    by_case: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for item in items:
        _, fields = item
        case_id = str(fields.get("case_id") or "")
        by_case[case_id].append(item)

    compacted: list[tuple[str, dict[str, Any]]] = []
    for case_items in by_case.values():
        compacted.extend(_compact_case_items(case_items))

    return sorted(compacted, key=_sort_key)


def run_worker_forever() -> None:
    check_redis_connectivity(decode_responses=True)
    _ensure_consumer_group()
    logger.info("outbound sender started: stream=%s group=%s consumer=%s", OUTBOUND_STREAM, OUTBOUND_CONSUMER_GROUP, OUTBOUND_CONSUMER_NAME)
    log_pipeline_tracing_status("outbound")

    stop_event = threading.Event()

    # Start reclaim thread
    reclaim_thread = _start_reclaim_thread(stop_event)

    try:
        while not stop_event.is_set():
            resp = run_with_redis_retries(
                lambda: _redis.xreadgroup(
                    groupname=OUTBOUND_CONSUMER_GROUP,
                    consumername=OUTBOUND_CONSUMER_NAME,
                    streams={OUTBOUND_STREAM: ">"},
                    count=OUTBOUND_BATCH_MAX,
                    block=OUTBOUND_BLOCK_MS,
                ),
                operation_name="outbound_xreadgroup",
            )
            messages = [(str(mid), fields) for _, batch in (resp or []) for mid, fields in batch]
            if not messages:
                continue

            grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
            for mid, fields in messages:
                grouped[str(fields.get("client_id") or "anonymous")].append((mid, fields))

            for client_id, items in grouped.items():
                selected_items = _compact_client_items(items)
                selected_ids = {mid for mid, _ in selected_items}

                # Detect chaos mode - essential for threading logic
                chaos_mode = _detect_chaos_mode(client_id, selected_items)
                if chaos_mode:
                    logger.debug(
                        "outbound chaos mode active: client_id=%s selected=%d",
                        client_id,
                        len(selected_items),
                    )

                ordered_items = sorted(selected_items, key=_delivery_order_key)

                # Ack/drop compacted-away records so they do not get redelivered.
                for mid, _ in items:
                    if mid not in selected_ids:
                        _ack_delete(mid)

                for mid, fields in ordered_items:
                    dedupe_key = str(fields.get("dedupe_key") or "")
                    seen_key = f"outbound:sent:{dedupe_key}" if dedupe_key else ""
                    if seen_key and _redis.exists(seen_key):
                        logger.info(
                            "outbound dedupe skip: kind=%s client_id=%s dedupe_key=%s msg_id=%s",
                            _kind(fields),
                            fields.get("client_id"),
                            dedupe_key,
                            mid,
                        )
                        _ack_delete(mid)
                        continue
                    try:
                        should_thread, target = _threading_decision(fields, chaos_mode)
                        kind = _kind(fields)
                        logger.info(
                            "outbound delivering: kind=%s client_id=%s channel=%s threaded=%s msg_id=%s",
                            kind,
                            fields.get("client_id"),
                            fields.get("channel"),
                            should_thread,
                            mid,
                        )
                        run_traced_stream_handler(
                            stream=OUTBOUND_STREAM,
                            group=OUTBOUND_CONSUMER_GROUP,
                            consumer=OUTBOUND_CONSUMER_NAME,
                            msg_id=mid,
                            fields=fields,
                            handler=lambda: _deliver(
                                fields,
                                reply_to_message_id=target if should_thread else None,
                            ),
                        )
                        if seen_key:
                            _redis.set(seen_key, "1", ex=OUTBOUND_DEDUPE_TTL_SEC)
                        _ack_delete(mid)
                        logger.info(
                            "outbound delivered: kind=%s client_id=%s msg_id=%s",
                            kind,
                            fields.get("client_id"),
                            mid,
                        )

                        # Mark as processed for idempotency
                        reclaim_logic.mark_processed(OUTBOUND_STREAM, mid)

                    except Exception as exc:
                        if seen_key:
                            _redis.delete(seen_key)
                        _dlq(mid, fields, str(exc))
                        _ack_delete(mid)
                        logger.exception(
                            "outbound delivery failed: kind=%s client_id=%s msg_id=%s",
                            _kind(fields),
                            fields.get("client_id"),
                            mid,
                        )
    except KeyboardInterrupt:
        logger.info("outbound sender received KeyboardInterrupt; shutting down")
    finally:
        stop_event.set()
        reclaim_thread.join(timeout=5.0)
        logger.info("outbound sender stopped")


if __name__ == "__main__":
    run_worker_forever()

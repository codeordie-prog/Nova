"""Ordered outbound sender worker."""

import json
import logging
import os
import re
import socket
from collections import defaultdict
from typing import Any

import redis
import requests
from dotenv import load_dotenv

from dermascan.shared.logger import get_logger

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_HTTP_TIMEOUT_S: float = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "20"))
TELEGRAM_TLS_VERIFY: str = os.getenv("TELEGRAM_TLS_VERIFY", "false")
TELEGRAM_CA_BUNDLE: str = os.getenv("TELEGRAM_CA_BUNDLE", "")

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
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

logger: logging.Logger = get_logger("agents")
_redis: redis.Redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)


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


def _send_telegram(chat_id: str, text: str, reply_to_message_id: int | None = None) -> None:
    """
    Send a message to a Telegram chat.

    reply_to_message_id logic is crucial - it preserves logical order for a better UX.
    
    
    Args:
        chat_id: The chat ID to send the message to.
        text: The text to send.
        reply_to_message_id: The message ID to reply to.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    verify: bool | str = True
    if TELEGRAM_CA_BUNDLE.strip():
        verify = TELEGRAM_CA_BUNDLE.strip()
    elif TELEGRAM_TLS_VERIFY.strip().lower() in {"0", "false", "no", "off"}:
        verify = False

    # Send message URL
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Create payload for each chunk in the split text
    for chunk in _split_telegram_text(text):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        
        resp = requests.post(url, json=payload, timeout=TELEGRAM_HTTP_TIMEOUT_S, verify=verify)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        if not payload.get("ok"):
            raise RuntimeError(f"telegram send non-ok: {payload}")


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


def _deliver(fields: dict[str, Any], reply_to_message_id: int | None = None) -> None:
    """
    Deliver a message to a channel.

    NB Only Telegram channel has been implemented : should be mutated later to include other channels.

    
    Args:
        fields: The fields to deliver.
    """
    channel = str(fields.get("channel") or "")
    case_id = str(fields.get("case_id") or "")
    text = str(fields.get("text") or "").strip()
    if not text:
        return

    # Send to Telegram
    if channel == "telegram":
        if not case_id:
            raise ValueError("missing case_id for telegram outbound")
        _send_telegram(case_id, text, reply_to_message_id=reply_to_message_id)
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
    return _to_int(fields.get("reply_to_message_id"))


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
    _redis.ping()
    _ensure_consumer_group()
    logger.info("outbound sender started: stream=%s group=%s consumer=%s", OUTBOUND_STREAM, OUTBOUND_CONSUMER_GROUP, OUTBOUND_CONSUMER_NAME)

    while True:
        resp = _redis.xreadgroup(
            groupname=OUTBOUND_CONSUMER_GROUP,
            consumername=OUTBOUND_CONSUMER_NAME,
            streams={OUTBOUND_STREAM: ">"},
            count=OUTBOUND_BATCH_MAX,
            block=OUTBOUND_BLOCK_MS,
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
                logger.debug("outbound chaos mode active: client_id=%s selected=%d", client_id, len(selected_items))

            ordered_items = sorted(selected_items, key=_delivery_order_key)

            # Ack/drop compacted-away records so they do not get redelivered.
            for mid, _ in items:
                if mid not in selected_ids:
                    _ack_delete(mid)

            for mid, fields in ordered_items:
                dedupe_key = str(fields.get("dedupe_key") or "")
                if dedupe_key:
                    seen_key = f"outbound:sent:{dedupe_key}"
                    if not _redis.set(seen_key, "1", nx=True, ex=OUTBOUND_DEDUPE_TTL_SEC):
                        _ack_delete(mid)
                        continue
                try:

                    # How do we reply - if chaos Mode reply to target, otherwise reply to message id
                    should_thread, target = _threading_decision(fields, chaos_mode)
                    _deliver(fields, reply_to_message_id=target if should_thread else None)
                    _ack_delete(mid)
                    
                except Exception as exc:
                    _dlq(mid, fields, str(exc))
                    _ack_delete(mid)
                    logger.exception("outbound delivery failed: msg_id=%s", mid)


if __name__ == "__main__":
    run_worker_forever()

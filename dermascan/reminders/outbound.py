"""
Author: Dermascan Team
Date: 11-1-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Enqueue proactive reminder messages on Redis streams (outbound + orchestrator signals).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from dermascan.reminders.morning_brief import build_morning_brief_text
from dermascan.reminders.signal_types import ORCHESTRATOR_SIGNAL_TYPE_MORNING_BRIEF
from dermascan.shared.logger import get_logger
from dermascan.shared.stream_pipeline_tracing import enrich_stream_fields, record_stream_produce

logger = get_logger(__name__)

OUTBOUND_STREAM: str = os.getenv("OUTBOUND_STREAM", "outbound:messages")
ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))


def enqueue_routine_reminder_text(
    redis_client: Any,
    *,
    client_id: str,
    channel: str,
    text: str,
    reminder_id: str,
) -> str:
    """
    Enqueue a deterministic routine reminder on the outbound stream.

    Args:
        redis_client: Decode-responses Redis client.
        client_id: User id on the channel.
        channel: Delivery channel (``whatsapp`` in v1).
        text: Message body.
        reminder_id: Reminder row uuid string for dedupe.

    Returns:
        str: Redis stream message id.

    Raises:
        RuntimeError: When the stream write fails.
    """
    body = text.strip()
    if not body:
        raise ValueError("routine reminder text is empty")
    request_id = f"reminder-routine-{reminder_id}"
    case_id = request_id
    sequence = redis_client.incr(f"outbound:seq:{client_id}")
    fields = enrich_stream_fields(
        {
            "client_id": client_id,
            "case_id": case_id,
            "channel": channel,
            "request_id": request_id,
            "kind": "reminder_routine",
            "must_deliver": "true",
            "priority": "15",
            "created_at_ms": str(int(time.time() * 1000)),
            "seq": str(sequence),
            "text": body,
            "dedupe_key": f"reminder_routine:{reminder_id}",
            "reply_to_message_id": "",
        }
    )
    try:
        msg_id = str(
            redis_client.xadd(
                OUTBOUND_STREAM,
                fields,
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
        )
    except Exception as exc:
        raise RuntimeError("failed to enqueue routine reminder outbound") from exc
    record_stream_produce(
        stream=OUTBOUND_STREAM,
        msg_id=msg_id,
        fields=fields,
        extra_metadata={"worker": "reminder_worker", "kind": "reminder_routine"},
    )
    return msg_id


def enqueue_morning_brief_signal(
    redis_client: Any,
    *,
    client_id: str,
    channel: str,
    reminder_id: str,
    timezone_name: str,
) -> str:
    """
    Enqueue an orchestrator signal to compose/send the morning brief.

    Args:
        redis_client: Decode-responses Redis client.
        client_id: User id on the channel.
        channel: Delivery channel.
        reminder_id: Morning briefing reminder row uuid.
        timezone_name: User IANA timezone.

    Returns:
        str: Redis stream message id.

    Raises:
        RuntimeError: When the stream write fails.
    """
    signal_id = uuid.uuid4().hex
    request_id = f"reminder-morning-{reminder_id}"
    case_id = request_id
    draft = build_morning_brief_text(timezone_name=timezone_name)
    payload = {"timezone": timezone_name, "draft": draft, "reminder_id": reminder_id}
    envelope: dict[str, str] = {
        "signal_type": ORCHESTRATOR_SIGNAL_TYPE_MORNING_BRIEF,
        "signal_id": signal_id,
        "request_id": request_id,
        "case_id": case_id,
        "client_id": client_id,
        "message_id": request_id,
        "channel": channel,
        "source_event_id": reminder_id,
        "payload": json.dumps(payload, default=str),
        "notes": "Morning briefing dispatched by reminder worker.",
        "confidence": "",
        "threshold": "",
    }
    fields = enrich_stream_fields(envelope)
    try:
        msg_id = str(
            redis_client.xadd(
                ORCHESTRATOR_SIGNAL_STREAM,
                fields,
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
        )
    except Exception as exc:
        raise RuntimeError("failed to enqueue morning brief signal") from exc
    record_stream_produce(
        stream=ORCHESTRATOR_SIGNAL_STREAM,
        msg_id=msg_id,
        fields=fields,
        extra_metadata={"worker": "reminder_worker", "signal_type": ORCHESTRATOR_SIGNAL_TYPE_MORNING_BRIEF},
    )
    logger.info(
        "morning brief signal enqueued: client_id=%s reminder_id=%s signal_id=%s",
        client_id,
        reminder_id,
        signal_id,
    )
    return msg_id


def enqueue_morning_brief_text_direct(
    redis_client: Any,
    *,
    client_id: str,
    channel: str,
    text: str,
    reminder_id: str,
) -> str:
    """
    Enqueue morning brief copy directly on outbound (pending-on-inbound delivery).

    Args:
        redis_client: Decode-responses Redis client.
        client_id: User id.
        channel: Delivery channel.
        text: Brief message body.
        reminder_id: Reminder row uuid for dedupe.

    Returns:
        str: Redis stream message id.

    Raises:
        RuntimeError: When the stream write fails.
    """
    request_id = f"reminder-morning-pending-{reminder_id}"
    case_id = request_id
    sequence = redis_client.incr(f"outbound:seq:{client_id}")
    fields = enrich_stream_fields(
        {
            "client_id": client_id,
            "case_id": case_id,
            "channel": channel,
            "request_id": request_id,
            "kind": "reminder_morning",
            "must_deliver": "true",
            "priority": "15",
            "created_at_ms": str(int(time.time() * 1000)),
            "seq": str(sequence),
            "text": text.strip(),
            "dedupe_key": f"reminder_morning:{reminder_id}",
            "reply_to_message_id": "",
        }
    )
    try:
        msg_id = str(
            redis_client.xadd(
                OUTBOUND_STREAM,
                fields,
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
        )
    except Exception as exc:
        raise RuntimeError("failed to enqueue pending morning brief") from exc
    record_stream_produce(
        stream=OUTBOUND_STREAM,
        msg_id=msg_id,
        fields=fields,
        extra_metadata={"worker": "reminder_worker", "kind": "reminder_morning"},
    )
    return msg_id

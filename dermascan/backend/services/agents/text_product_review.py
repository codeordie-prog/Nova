"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
License: MIT

Text-only product review dispatch (cosmetic pivot Step 5.1).

Skips MedGemma label read when intake has ``product_ref_text`` and no product
image. Emits ``product_review_completed`` on ``orchestrator:signals`` so the
existing orchestrator signal handler runs ``run_product_signal_rules_flow``.
"""


from __future__ import annotations

import json
import os
import uuid
from typing import Any, Mapping

from dermascan.shared.cosmetic_feature_flags import (
    cosmetic_pivot_enabled,
    cosmetic_text_product_review_enabled,
)
from dermascan.shared.intake_session import IntakeSession, has_product_image
from dermascan.shared.logger import get_logger
from dermascan.shared.stream_pipeline_tracing import enrich_stream_fields, record_stream_produce

logger = get_logger(__name__)

ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
_TEXT_PRODUCT_DEDUPE_PREFIX: str = "audit:text_product:"
_TEXT_PRODUCT_DEDUPE_TTL_SEC: int = max(
    3600,
    int(os.getenv("TEXT_PRODUCT_REVIEW_DEDUPE_TTL_SEC", str(7 * 24 * 3600))),
)


def text_product_review_eligible(session: IntakeSession) -> bool:
    """
    Return True when text-only product review may run for the session.

    Args:
        session: Active intake session.

    Returns:
        bool: True when flags are on, product name is set, and no label image exists.
    """
    if not cosmetic_pivot_enabled() or not cosmetic_text_product_review_enabled():
        return False
    if has_product_image(session):
        return False
    return bool(session.product_ref_text.strip())


def text_product_dedupe_key(case_id: str) -> str:
    """
    Build Redis dedupe key for a text-only product review dispatch.

    Args:
        case_id: Intake case identifier.

    Returns:
        str: Redis key string.
    """
    normalized = str(case_id or "anonymous").strip() or "anonymous"
    return f"{_TEXT_PRODUCT_DEDUPE_PREFIX}{normalized}"


def is_text_product_review_enqueued(redis_client: Any, case_id: str) -> bool:
    """
    Return True when a text-only product signal was already dispatched for the case.

    Args:
        redis_client: Redis client.
        case_id: Intake case identifier.

    Returns:
        bool: True when dedupe marker exists.
    """
    if redis_client is None:
        return False
    try:
        return bool(redis_client.get(text_product_dedupe_key(case_id)))
    except Exception:
        logger.exception("failed to read text product dedupe key: case_id=%s", case_id)
        return False


def build_text_product_signal_payload(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    collected_context: str = "",
) -> dict[str, Any]:
    """
    Build orchestrator signal payload for a text-only product rules flow.

    Args:
        session: Intake session with ``product_ref_text``.
        profile: Skin profile mapping for optional description context.
        collected_context: Optional supplemental audit context string.

    Returns:
        dict[str, Any]: Payload consumed by ``run_product_signal_rules_flow``.
    """
    description_parts: list[str] = []
    if session.primary_concern.strip():
        description_parts.append(session.primary_concern.strip())
    if collected_context.strip():
        description_parts.append(collected_context.strip())
    if session.collected_context_summary.strip():
        description_parts.append(session.collected_context_summary.strip())

    goals = profile.get("current_goals")
    if isinstance(goals, list) and goals:
        goals_text = ", ".join(str(item).strip() for item in goals if str(item).strip())
        if goals_text:
            description_parts.append(f"Goals: {goals_text}")

    return {
        "mode": "product_review",
        "review_type": "product",
        "product_name": session.product_ref_text.strip(),
        "description": " | ".join(description_parts),
        "text_only": True,
        "personalization_mode": session.personalization_mode,
        "skin_snapshot_skipped": session.skin_snapshot_skipped,
        "escalation": False,
    }


def dispatch_text_product_review_signal(
    redis_client: Any,
    *,
    session: IntakeSession,
    profile: Mapping[str, Any],
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    collected_context: str = "",
) -> dict[str, Any]:
    """
    Emit ``product_review_completed`` for a text-only product review.

    Args:
        redis_client: Redis client for stream write and dedupe.
        session: Intake session with product reference text.
        profile: Skin profile mapping.
        request_id: Current orchestrator turn request id.
        case_id: Case identifier.
        client_id: Client identifier.
        message_id: Message identifier.
        channel: Delivery channel.
        collected_context: Optional supplemental context from the tool call.

    Returns:
        dict[str, Any]: Dispatch result with status and instruction hints.

    Raises:
        RuntimeError: When Redis is unavailable or the stream write fails.
    """
    if redis_client is None:
        raise RuntimeError("REDIS_URL is required for text product review dispatch")

    dedupe_key = text_product_dedupe_key(case_id)
    if not redis_client.set(dedupe_key, "1", nx=True, ex=_TEXT_PRODUCT_DEDUPE_TTL_SEC):
        logger.info(
            "text product review already dispatched: case_id=%s client_id=%s",
            case_id,
            client_id,
        )
        return {
            "status": "already_queued",
            "audit_type": "product",
            "text_only": True,
            "instruction": (
                "Text-only product review was already dispatched. Do NOT call enqueue again. "
                "Tell the user their product check is in progress."
            ),
        }

    signal_id = str(uuid.uuid4())
    payload = build_text_product_signal_payload(
        session,
        profile,
        collected_context=collected_context,
    )
    signal_envelope: dict[str, Any] = {
        "signal_type": "product_review_completed",
        "signal_id": signal_id,
        "request_id": request_id,
        "case_id": case_id,
        "client_id": client_id,
        "message_id": message_id or request_id,
        "channel": channel,
        "source_event_id": "",
        "payload": json.dumps(payload, default=str),
        "notes": "Text-only product review dispatched (no MedGemma label read).",
        "confidence": "",
        "threshold": "",
    }
    signal_fields = enrich_stream_fields(signal_envelope)
    try:
        msg_id = str(
            redis_client.xadd(
                ORCHESTRATOR_SIGNAL_STREAM,
                signal_fields,
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
        )
    except Exception as exc:
        redis_client.delete(dedupe_key)
        raise RuntimeError(f"failed to enqueue text product review signal: {exc}") from exc

    record_stream_produce(
        stream=ORCHESTRATOR_SIGNAL_STREAM,
        msg_id=msg_id,
        fields=signal_fields,
        extra_metadata={
            "worker": "orchestrator_tool",
            "signal_type": "product_review_completed",
            "text_only": "true",
        },
    )
    logger.info(
        "text product review signal dispatched: case_id=%s client_id=%s product_name=%s signal_id=%s",
        case_id,
        client_id,
        payload.get("product_name"),
        signal_id,
    )
    return {
        "status": "queued",
        "audit_type": "product",
        "text_only": True,
        "signal_id": signal_id,
        "product_name": payload.get("product_name", ""),
        "instruction": (
            "Text-only product review dispatched to the specialist rules flow. "
            "Tell the user their product check is in progress."
        ),
    }

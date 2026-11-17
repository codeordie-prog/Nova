"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-10-20
Licence: PolyForm Noncommercial License 1.0.0

Redis-backed anchors linking client media and completed AI reviews to dermatologist escalation.

Image anchors are written at ingress and intake registration. Review completion is recorded
after MedGemma inference or when the orchestrator delivers a product/skin rules report.
"""


from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

_ESCALATION_ANCHOR_KEY_TEMPLATE: str = "escalation:anchor:client:{client_id}"
_ESCALATION_ANCHOR_TTL_SECONDS: int = max(
    3600,
    int(os.getenv("ESCALATION_ANCHOR_TTL_SECONDS", str(60 * 24 * 3600))),
)


def escalation_anchor_key(client_id: str) -> str:
    """
    Build Redis key for a client's escalation anchor document.

    Args:
        client_id: Stable client identifier.

    Returns:
        str: Redis key string.
    """
    normalized = str(client_id or "").strip() or "anonymous"
    return _ESCALATION_ANCHOR_KEY_TEMPLATE.format(client_id=normalized)


@dataclass(frozen=True)
class ImageAnchorSlot:
    """Registered or ingress image pointer for specialist handoff."""

    anchor_request_id: str
    case_id: str
    message_id: str
    channel: str
    image_blob_path: str
    category: str
    updated_at_ms: int


@dataclass(frozen=True)
class CompletedReviewAnchor:
    """Last completed AI review eligible for user-requested dermatologist escalation."""

    mode: str
    anchor_request_id: str
    case_id: str
    message_id: str
    channel: str
    image_blob_path: str
    diagnosis_text: str
    risk_tier: str
    review_type: str
    completed_at_ms: int
    confidence: float | None = None
    summary: Any = None
    treatment: Any = None
    product_name: str = ""
    description: str = ""
    indications: str = ""
    safety_in_pregnancy: str = ""
    contraindications: str = ""
    how_to_use: str = ""
    special_instructions: str = ""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_document(redis_client: Any, client_id: str) -> dict[str, Any]:
    if redis_client is None:
        return {}
    key = escalation_anchor_key(client_id)
    try:
        raw = redis_client.get(key)
    except Exception:
        logger.exception("escalation anchor get failed: client_id=%s", client_id)
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _save_document(redis_client: Any, client_id: str, document: dict[str, Any]) -> None:
    if redis_client is None:
        return
    key = escalation_anchor_key(client_id)
    try:
        redis_client.setex(key, _ESCALATION_ANCHOR_TTL_SECONDS, json.dumps(document, default=str))
    except Exception:
        logger.exception("escalation anchor set failed: client_id=%s", client_id)


def load_completed_review_anchor(
    redis_client: Any,
    client_id: str,
) -> CompletedReviewAnchor | None:
    """
    Load the last completed AI review anchor for a client.

    Args:
        redis_client: Redis client.
        client_id: Stable client identifier.

    Returns:
        CompletedReviewAnchor | None: Parsed anchor when present.
    """
    document = _load_document(redis_client, client_id)
    block = document.get("last_completed_review")
    if not isinstance(block, dict):
        return None
    anchor_request_id = str(block.get("anchor_request_id") or "").strip()
    case_id = str(block.get("case_id") or "").strip()
    if not anchor_request_id or not case_id:
        return None
    confidence_raw = block.get("confidence")
    confidence: float | None
    try:
        confidence = float(confidence_raw) if confidence_raw is not None and str(confidence_raw).strip() else None
    except (TypeError, ValueError):
        confidence = None
    return CompletedReviewAnchor(
        mode=str(block.get("mode") or "diagnose").strip(),
        anchor_request_id=anchor_request_id,
        case_id=case_id,
        message_id=str(block.get("message_id") or "").strip(),
        channel=str(block.get("channel") or "").strip(),
        image_blob_path=str(block.get("image_blob_path") or "").strip(),
        diagnosis_text=str(block.get("diagnosis_text") or "").strip(),
        risk_tier=str(block.get("risk_tier") or "cosmetic").strip(),
        review_type=str(block.get("review_type") or "").strip(),
        completed_at_ms=int(block.get("completed_at_ms") or 0),
        confidence=confidence,
        summary=block.get("summary"),
        treatment=block.get("treatment"),
        product_name=str(block.get("product_name") or "").strip(),
        description=str(block.get("description") or "").strip(),
        indications=str(block.get("indications") or "").strip(),
        safety_in_pregnancy=str(block.get("safety_in_pregnancy") or "").strip(),
        contraindications=str(block.get("contraindications") or "").strip(),
        how_to_use=str(block.get("how_to_use") or "").strip(),
        special_instructions=str(block.get("special_instructions") or "").strip(),
    )


def record_ingress_media_anchor(
    redis_client: Any,
    *,
    client_id: str,
    anchor_request_id: str,
    case_id: str,
    message_id: str = "",
    channel: str = "",
    image_blob_path: str = "",
) -> None:
    """
    Record the latest inbound image message before intake categorization.

    Args:
        redis_client: Redis client.
        client_id: Stable client identifier.
        anchor_request_id: Ingress request id for the image message.
        case_id: Case id from ingress envelope.
        message_id: Source message id.
        channel: Ingress channel.
        image_blob_path: Durable blob path when known.
    """
    cid = str(client_id or "").strip()
    rid = str(anchor_request_id or "").strip()
    if not cid or not rid:
        return
    document = _load_document(redis_client, cid)
    document["latest_ingress_media"] = {
        "anchor_request_id": rid,
        "case_id": str(case_id or rid).strip(),
        "message_id": str(message_id or "").strip(),
        "channel": str(channel or "").strip(),
        "image_blob_path": str(image_blob_path or "").strip(),
        "updated_at_ms": _now_ms(),
    }
    _save_document(redis_client, cid, document)


def record_image_anchor(
    redis_client: Any,
    *,
    client_id: str,
    category: str,
    anchor_request_id: str,
    case_id: str,
    message_id: str = "",
    channel: str = "",
    image_blob_path: str = "",
) -> None:
    """
    Upsert a skin or product image anchor for a client.

    Args:
        redis_client: Redis client.
        client_id: Stable client identifier.
        category: ``skin`` or ``product``.
        anchor_request_id: Ingress request id for the image message.
        case_id: Case id from ingress envelope.
        message_id: Source message id.
        channel: Ingress channel.
        image_blob_path: Durable blob path when known.
    """
    cid = str(client_id or "").strip()
    rid = str(anchor_request_id or "").strip()
    if not cid or not rid:
        return
    cat = str(category or "").strip().lower()
    if cat not in {"skin", "product"}:
        return
    document = _load_document(redis_client, cid)
    slot_key = f"{cat}_image"
    document[slot_key] = {
        "anchor_request_id": rid,
        "case_id": str(case_id or rid).strip(),
        "message_id": str(message_id or "").strip(),
        "channel": str(channel or "").strip(),
        "image_blob_path": str(image_blob_path or "").strip(),
        "category": cat,
        "updated_at_ms": _now_ms(),
    }
    _save_document(redis_client, cid, document)


def record_completed_review_anchor(
    redis_client: Any,
    *,
    client_id: str,
    mode: str,
    anchor_request_id: str,
    case_id: str,
    message_id: str = "",
    channel: str = "",
    image_blob_path: str = "",
    diagnosis_text: str = "",
    risk_tier: str = "cosmetic",
    review_type: str = "",
    confidence: float | None = None,
    summary: Any = None,
    treatment: Any = None,
    product_name: str = "",
    description: str = "",
    indications: str = "",
    safety_in_pregnancy: str = "",
    contraindications: str = "",
    how_to_use: str = "",
    special_instructions: str = "",
) -> None:
    """
    Persist the latest completed AI review eligible for dermatologist escalation.

    Args:
        redis_client: Redis client.
        client_id: Stable client identifier.
        mode: ``diagnose`` or ``product_review``.
        anchor_request_id: Audit request id (MedGemma or orchestrator turn).
        case_id: Case id tied to the review.
        message_id: Message id for relay tracking.
        channel: Ingress channel.
        image_blob_path: Image path for dashboard serving.
        diagnosis_text: Internal clinical or product summary text.
        risk_tier: Normalized risk tier when applicable.
        review_type: Human-readable review type label.
        confidence: Optional vision confidence.
        summary: Structured summary fragment.
        treatment: Structured treatment fragment.
        product_name: Product name for product reviews.
        description: Product description for product reviews.
        indications: Product indications field.
        safety_in_pregnancy: Pregnancy safety notes.
        contraindications: Contraindications text.
        how_to_use: Usage instructions.
        special_instructions: Special instructions text.
    """
    cid = str(client_id or "").strip()
    rid = str(anchor_request_id or "").strip()
    normalized_case = str(case_id or "").strip()
    if not cid or not rid or not normalized_case:
        return
    normalized_mode = str(mode or "diagnose").strip().lower()
    if normalized_mode not in {"diagnose", "product_review", "hybrid"}:
        normalized_mode = "diagnose"
    document = _load_document(redis_client, cid)
    document["last_completed_review"] = {
        "mode": normalized_mode,
        "anchor_request_id": rid,
        "case_id": normalized_case,
        "message_id": str(message_id or "").strip(),
        "channel": str(channel or "").strip(),
        "image_blob_path": str(image_blob_path or "").strip(),
        "diagnosis_text": str(diagnosis_text or "").strip(),
        "risk_tier": str(risk_tier or "cosmetic").strip(),
        "review_type": str(review_type or "").strip(),
        "completed_at_ms": _now_ms(),
        "confidence": "" if confidence is None else str(confidence),
        "summary": summary if isinstance(summary, (dict, list)) else summary,
        "treatment": treatment if isinstance(treatment, (dict, list)) else treatment,
        "product_name": str(product_name or "").strip(),
        "description": str(description or "").strip(),
        "indications": str(indications or "").strip(),
        "safety_in_pregnancy": str(safety_in_pregnancy or "").strip(),
        "contraindications": str(contraindications or "").strip(),
        "how_to_use": str(how_to_use or "").strip(),
        "special_instructions": str(special_instructions or "").strip(),
    }
    _save_document(redis_client, cid, document)


def record_completed_review_from_medgemma_fields(
    redis_client: Any,
    *,
    client_id: str,
    mode: str,
    anchor_request_id: str,
    case_id: str,
    message_id: str,
    channel: str,
    image_blob_path: str,
    base_fields: Mapping[str, Any],
    diagnosis_text: str,
    confidence: float | None,
) -> None:
    """
    Record review completion from MedGemma post-processing structured fields.

    Args:
        redis_client: Redis client.
        client_id: Client identifier.
        mode: Routing mode after inference.
        anchor_request_id: MedGemma job request id.
        case_id: Case id.
        message_id: Message id.
        channel: Channel name.
        image_blob_path: Blob path from job entry.
        base_fields: Parsed structured output mapping.
        diagnosis_text: Resolved internal diagnosis text.
        confidence: Vision confidence score.
    """
    review_type = str(base_fields.get("review_type") or "").strip()
    if not review_type:
        review_type = "Product review" if mode == "product_review" else "Skin review"
    record_completed_review_anchor(
        redis_client,
        client_id=client_id,
        mode=mode,
        anchor_request_id=anchor_request_id,
        case_id=case_id,
        message_id=message_id,
        channel=channel,
        image_blob_path=image_blob_path,
        diagnosis_text=diagnosis_text,
        risk_tier=str(base_fields.get("risk_tier") or "cosmetic"),
        review_type=review_type,
        confidence=confidence,
        summary=base_fields.get("summary"),
        treatment=base_fields.get("treatment"),
        product_name=str(base_fields.get("product_name") or ""),
        description=str(base_fields.get("description") or ""),
        indications=str(base_fields.get("indications") or ""),
        safety_in_pregnancy=str(base_fields.get("safety_in_pregnancy") or ""),
        contraindications=str(base_fields.get("contraindications") or ""),
        how_to_use=str(base_fields.get("how_to_use") or ""),
        special_instructions=str(base_fields.get("special_instructions") or ""),
    )


def record_completed_review_from_signal_payload(
    redis_client: Any,
    *,
    client_id: str,
    anchor_request_id: str,
    case_id: str,
    message_id: str,
    channel: str,
    payload: Mapping[str, Any],
    image_blob_path: str = "",
) -> None:
    """
    Record review completion from an orchestrator signal payload (rules / text product).

    Args:
        redis_client: Redis client.
        client_id: Client identifier.
        anchor_request_id: Signal request id.
        case_id: Case id.
        message_id: Message id.
        channel: Channel name.
        payload: Parsed signal payload mapping.
        image_blob_path: Optional blob path override.
    """
    mode = str(payload.get("mode") or "diagnose").strip().lower()
    if mode not in {"diagnose", "product_review", "hybrid"}:
        mode = "product_review" if str(payload.get("review_type") or "").lower().startswith("product") else "diagnose"
    diagnosis = str(
        payload.get("diagnosis_text")
        or payload.get("diagnosis")
        or payload.get("primary_observation")
        or payload.get("product_name")
        or ""
    ).strip()
    record_completed_review_anchor(
        redis_client,
        client_id=client_id,
        mode=mode,
        anchor_request_id=anchor_request_id,
        case_id=case_id,
        message_id=message_id,
        channel=channel,
        image_blob_path=image_blob_path,
        diagnosis_text=diagnosis,
        risk_tier=str(payload.get("risk_tier") or "cosmetic"),
        review_type=str(payload.get("review_type") or ""),
        confidence=None,
        summary=payload.get("summary"),
        treatment=payload.get("treatment"),
        product_name=str(payload.get("product_name") or ""),
        description=str(payload.get("description") or ""),
        indications=str(payload.get("indications") or ""),
        safety_in_pregnancy=str(payload.get("safety_in_pregnancy") or ""),
        contraindications=str(payload.get("contraindications") or ""),
        how_to_use=str(payload.get("how_to_use") or ""),
        special_instructions=str(payload.get("special_instructions") or ""),
    )


def load_latest_ingress_media_anchor(
    redis_client: Any,
    client_id: str,
) -> dict[str, str] | None:
    """
    Load the latest uncategorized ingress media anchor for a client.

    Args:
        redis_client: Redis client.
        client_id: Client identifier.

    Returns:
        dict[str, str] | None: Anchor mapping when present.
    """
    document = _load_document(redis_client, client_id)
    block = document.get("latest_ingress_media")
    if not isinstance(block, dict):
        return None
    anchor_request_id = str(block.get("anchor_request_id") or "").strip()
    if not anchor_request_id:
        return None
    return {
        "anchor_request_id": anchor_request_id,
        "case_id": str(block.get("case_id") or "").strip(),
        "message_id": str(block.get("message_id") or "").strip(),
        "channel": str(block.get("channel") or "").strip(),
        "image_blob_path": str(block.get("image_blob_path") or "").strip(),
    }


def dermatologist_escalation_readiness(
    redis_client: Any,
    *,
    client_id: str,
    has_pending_consent_offer: bool,
) -> dict[str, Any]:
    """
    Summarize whether a client may request human dermatologist escalation.

    Args:
        redis_client: Redis client.
        client_id: Client identifier.
        has_pending_consent_offer: True when consent offer key exists.

    Returns:
        dict[str, Any]: Readiness flags and agent instruction hints.
    """
    completed = load_completed_review_anchor(redis_client, client_id)
    ready = completed is not None and completed.completed_at_ms > 0
    mode = completed.mode if completed else ""
    ingress_media = load_latest_ingress_media_anchor(redis_client, client_id)
    return {
        "ready_for_dermatologist": ready or has_pending_consent_offer,
        "pending_consent_offer": has_pending_consent_offer,
        "last_review_mode": mode or None,
        "last_review_request_id": completed.anchor_request_id if completed else None,
        "requires_ai_review_first": not ready and not has_pending_consent_offer,
        "latest_ingress_media_request_id": (
            ingress_media.get("anchor_request_id") if ingress_media else None
        ),
    }

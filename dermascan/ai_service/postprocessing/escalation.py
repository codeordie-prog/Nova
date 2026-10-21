"""
Author: Dermascan Team
Date: 2026-4-29
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: This module contains the implementation of the Escalation logic post-processing

The idea relies on the confidence score threshold bar set for the diagnosis model.

If the calculated confidence score falls below the threshold, the case is ESCALATED
to a certified Physician for further assesment.

In-addition to this, there are other routing logic that involve passing certain cases to
the Physician irregardless of the confidence score - This is purposefully to improve
the quality of the product as time goes by.
"""

import logging
import os
import json

from dotenv import load_dotenv
from typing import Any, Mapping
from dataclasses import dataclass

from redis import Redis

from dermascan.ai_service.postprocessing.consent_escalation import resolve_consent_escalation_decision
from dermascan.ai_service.postprocessing.medgemma_structured_output import (
    extract_structured_fields,
    resolve_internal_diagnosis_text,
)
from dermascan.shared.cosmetic_feature_flags import cosmetic_consent_escalation_active
from dermascan.shared.escalation_image import cache_escalation_image_path, resolve_escalation_image_blob_path
from dermascan.shared.intake_session import (
    IntakeImageCategory,
    IntakeSession,
    build_collected_context_summary,
)
from dermascan.shared.escalation_anchor import (
    load_completed_review_anchor,
    record_completed_review_from_medgemma_fields,
)
from dermascan.shared.skin_audit_store import load_latest_skin_audit_envelope
from dermascan.shared.stream_pipeline_tracing import enrich_stream_fields, record_stream_produce

# load environment variables
load_dotenv()

# Get environment variables
_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_ORCHESTRATOR_SIGNAL_STREAM: str = os.environ.get("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
_EVENTS_STREAM: str = os.environ.get("EVENTS_STREAM","events:stream")
_ESCALATION_STREAM: str = os.environ.get("ESCALATION_STREAM", "escalation:jobs")
_EVENTS_STREAM_MAXLEN: int = int(os.environ.get("WORKERS_STREAM_MAXLEN", "5000"))
_DIAGNOSE_CONFIDENCE_THRESHOLD: float = float(os.environ.get("DIAGNOSE_CONFIDENCE_THRESHOLD", "0.75"))
_PRODUCT_REVIEW_CONFIDENCE_THRESHOLD: float = float(os.environ.get("PRODUCT_REVIEW_CONFIDENCE_THRESHOLD", "0.60"))
_ESCALATION_DLQ_STREAM: str = os.environ.get("ESCALATION_DLQ_STREAM", "escalation:dlq")
_EVENTS_DLQ_STREAM: str = os.environ.get("EVENTS_DLQ_STREAM","events:dlq")
_CONSENT_OFFER_CLIENT_KEY_TEMPLATE: str = "escalation:consent_offer:client:{client_id}"
_CONSENT_OFFER_LEGACY_CASE_KEY_TEMPLATE: str = "escalation:consent_offer:{case_id}"
_USER_ESCALATION_DEDUPE_KEY_TEMPLATE: str = "escalation:user_requested:client:{client_id}"
_INGRESS_PAYLOAD_CACHE_PREFIX: str = "orchestrator:ingress_payload"
_CONSENT_OFFER_TTL_SECONDS: int = max(
    3600,
    int(os.environ.get("ESCALATION_CONSENT_OFFER_TTL_SECONDS", str(7 * 24 * 3600))),
)

# Client-facing copy when inference is below threshold (orchestrator must not surface model output).
ESCALATION_CLIENT_MESSAGE: str = os.environ.get(
    "ESCALATION_CLIENT_MESSAGE",
    "Your case requires a detailed review by a skin specialist. "
    "We have contacted them for you, please wait for their response.",
)
ESCALATION_DEFERRED_CLIENT_MESSAGE: str = os.environ.get(
    "ESCALATION_DEFERRED_CLIENT_MESSAGE",
    "Thanks for your patience. A skin specialist is reviewing your case and "
    "will follow up shortly with their assessment.",
)


_LOGGER: logging.Logger = logging.getLogger("dermascan.ai_service.postprocessing.escalation")

# Initialize REDIS client
_redis: Redis = Redis.from_url(_REDIS_URL, decode_responses=True)

@dataclass
class _JobEntry:
    """
    One Job collected during batch window
    """
    job_id: str
    prompt: str
    image_bytes: bytes
    msg_id: str
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    group_id: str = ""
    group_expected_count: int = 0
    image_blob_path: str = ""


def _to_str(value: Any) -> str:
    """
    Convert a value to a string, handling bytes and None.
    
    Args:
        value: The value to convert
        
    Returns:
        The string representation of the value
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return "" if value is None else str(value)


def _stream_json(value: Any) -> str:
    """Serialize dict/list fields to JSON for the Redis stream; pass scalars through."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return _to_str(value)


def is_escalated_signal_type(signal_type: str) -> bool:
    """
    Return True for low-confidence ``{mode}_escalated`` orchestrator signals.

    Excludes physician-reviewed and relay-deferred variants.
    """
    normalized = str(signal_type or "").strip().lower()
    if not normalized.endswith("_escalated"):
        return False
    return not normalized.endswith("_physician_reviewed")


def is_escalation_deferred_signal_type(signal_type: str) -> bool:
    """Return True when escalation relay failed and a deferred client notice is pending."""
    return str(signal_type or "").strip().lower().endswith("_escalation_deferred")


def resolve_client_message_for_signal(fields: dict[str, Any]) -> str | None:
    """
    Resolve a fixed client-safe reply for escalation-related orchestrator signals.

    Returns None when the signal should follow the normal rules/LLM composition path.
    """
    signal_type = str(fields.get("signal_type") or "")
    if is_escalation_deferred_signal_type(signal_type):
        return ESCALATION_DEFERRED_CLIENT_MESSAGE
    if is_escalated_signal_type(signal_type):
        custom = str(fields.get("client_message") or "").strip()
        return custom or ESCALATION_CLIENT_MESSAGE
    return None


def _consent_offer_key_for_client(client_id: str) -> str:
    """
    Build Redis key for a pending consent-gated escalation offer for a client.

    Offers are keyed by ``client_id`` because each inbound message receives a
    new ``case_id``; consent acceptance arrives on a later turn.

    Args:
        client_id: Stable client identifier from ingress.

    Returns:
        str: Redis key string.
    """
    normalized = str(client_id or "").strip() or "anonymous"
    return _CONSENT_OFFER_CLIENT_KEY_TEMPLATE.format(client_id=normalized)


def _consent_offer_key_legacy_case(case_id: str) -> str:
    """
    Build legacy Redis key keyed by skin-audit ``case_id`` (pre-8.2a).

    Args:
        case_id: Skin audit case identifier.

    Returns:
        str: Legacy Redis key string.
    """
    normalized = str(case_id or "").strip() or "anonymous"
    return _CONSENT_OFFER_LEGACY_CASE_KEY_TEMPLATE.format(case_id=normalized)


def _load_consent_offer_raw(
    *,
    client_id: str,
    case_id: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Load a cached consent offer, preferring the client key with legacy fallback.

    Args:
        client_id: Stable client identifier.
        case_id: Optional current-turn case id for legacy key lookup.

    Returns:
        tuple[str | None, str | None]: Redis key and payload text when found.
    """
    client = str(client_id or "").strip()
    if client:
        client_key = _consent_offer_key_for_client(client)
        try:
            raw = _redis.get(client_key)
        except Exception:
            raw = None
        if raw:
            return client_key, str(raw)

    legacy_case = str(case_id or "").strip()
    if legacy_case:
        legacy_key = _consent_offer_key_legacy_case(legacy_case)
        try:
            raw = _redis.get(legacy_key)
        except Exception:
            raw = None
        if raw:
            return legacy_key, str(raw)

    return None, None


def _cache_consent_escalation_offer(
    entry: _JobEntry,
    *,
    mode: str,
    confidence: float | None,
    threshold_used: float,
    base_fields: dict[str, str],
    text: str,
) -> None:
    """
    Cache escalation job fields until the user explicitly consents.

    Args:
        entry: Source inference job entry.
        mode: Routing mode string.
        confidence: MedGemma confidence score.
        threshold_used: Threshold applied for the mode.
        base_fields: Parsed structured fields from inference.
        text: Raw model response text.
    """
    case_id = str(entry.case_id or "").strip()
    client_id = str(entry.client_id or "").strip()
    if not client_id:
        return
    payload = {
        "request_id": entry.request_id,
        "case_id": case_id,
        "client_id": entry.client_id,
        "message_id": entry.message_id,
        "channel": entry.channel,
        "source_agent": "medgemma",
        "mode": mode,
        "reason": "risk_tier_watch_consent",
        "confidence": "" if confidence is None else str(confidence),
        "threshold": str(threshold_used),
        "diagnosis_text": resolve_internal_diagnosis_text(base_fields) or text,
        "requires_human_review": "true",
        "product_name": base_fields.get("product_name", ""),
        "description": base_fields.get("description", ""),
        "indications": base_fields.get("indications", ""),
        "safety_in_pregnancy": base_fields.get("safety_in_pregnancy", ""),
        "contraindications": base_fields.get("contraindications", ""),
        "how_to_use": base_fields.get("how_to_use", ""),
        "special_instructions": base_fields.get("special_instructions", ""),
        "image_blob_path": entry.image_blob_path or "",
        "review_type": base_fields.get("review_type", ""),
        "rationale": base_fields.get("rationale", ""),
        "summary": _stream_json(base_fields.get("summary")),
        "treatment": _stream_json(base_fields.get("treatment")),
        "risk_tier": base_fields.get("risk_tier", "watch"),
    }
    try:
        serialized = json.dumps(payload)
        _redis.setex(
            _consent_offer_key_for_client(client_id),
            _CONSENT_OFFER_TTL_SECONDS,
            serialized,
        )
        if case_id:
            _redis.setex(
                _consent_offer_key_legacy_case(case_id),
                _CONSENT_OFFER_TTL_SECONDS,
                serialized,
            )
    except Exception:
        pass


def dispatch_consent_escalation(
    client_id: str,
    *,
    case_id: str | None = None,
) -> bool:
    """
    Push a cached consent-offer escalation job after explicit user acceptance.

    Args:
        client_id: Stable client identifier tied to the pending offer.
        case_id: Optional current-turn case id for legacy offer lookup.

    Returns:
        bool: True when a job was dispatched to ``escalation:jobs``.
    """
    key, raw = _load_consent_offer_raw(client_id=client_id, case_id=case_id)
    if not key or not raw:
        _LOGGER.warning(
            "dispatch_consent_escalation: no cached offer for client_id=%s case_id=%s",
            client_id,
            case_id,
        )
        return False
    try:
        fields = json.loads(_to_str(raw))
    except Exception as exc:
        _LOGGER.exception(
            "dispatch_consent_escalation: offer payload not valid JSON for key=%s: %s",
            key,
            exc,
        )
        return False
    if not isinstance(fields, dict):
        _LOGGER.warning(
            "dispatch_consent_escalation: offer payload not a dict for key=%s (type=%s)",
            key,
            type(fields).__name__,
        )
        return False
    try:
        _redis.xadd(
            _ESCALATION_STREAM,
            {k: _to_str(v) for k, v in fields.items()},
            maxlen=_EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
        _redis.delete(key)
        legacy_case = str(fields.get("case_id") or "").strip()
        if legacy_case:
            _redis.delete(_consent_offer_key_legacy_case(legacy_case))
    except Exception as exc:
        _LOGGER.exception(
            "dispatch_consent_escalation: xadd/delete failed for client_id=%s: %s",
            client_id,
            exc,
        )
        return False
    return True


def has_pending_consent_escalation_offer(
    client_id: str,
    *,
    case_id: str | None = None,
) -> bool:
    """
    Return True when a consent-gated escalation offer is cached for the client.

    Args:
        client_id: Stable client identifier.
        case_id: Optional current-turn case id for legacy key lookup.

    Returns:
        bool: True when an offer key exists in Redis.
    """
    key, _ = _load_consent_offer_raw(client_id=client_id, case_id=case_id)
    return bool(key)


@dataclass(frozen=True)
class UserEscalationContext:
    """
    Resolved client context for on-demand specialist escalation.

    Attributes:
        audit_case_id: Skin-audit case id used for dashboard/image lookup.
        audit_request_id: MedGemma audit request id for image resolution.
        message_id: Current-turn message id for relay tracking.
        channel: Ingress channel for the current turn.
        image_blob_path: Durable Firebase blob path when resolved.
        diagnosis_text: Internal clinical hypothesis or concern text for specialists.
        escalation_context: Intake/profile summary for specialist review.
        risk_tier: Normalized risk tier from the latest skin audit.
        summary: Structured summary fragment when available.
        treatment: Structured treatment fragment when available.
        confidence: Vision confidence from the latest skin audit, if known.
        mode: Escalation job mode (``diagnose`` or ``product_review``).
        review_type: Human-readable review label for the platform handoff.
        product_name: Product name when mode is product review.
        description: Product description field for product review jobs.
        indications: Product indications field.
        safety_in_pregnancy: Pregnancy safety notes.
        contraindications: Contraindications text.
        how_to_use: Usage instructions.
        special_instructions: Special instructions text.
    """

    audit_case_id: str
    audit_request_id: str
    message_id: str
    channel: str
    image_blob_path: str
    diagnosis_text: str
    escalation_context: str
    risk_tier: str
    summary: Any
    treatment: Any
    confidence: float | None
    mode: str = "diagnose"
    review_type: str = "Skin review"
    product_name: str = ""
    description: str = ""
    indications: str = ""
    safety_in_pregnancy: str = ""
    contraindications: str = ""
    how_to_use: str = ""
    special_instructions: str = ""


def _load_ingress_envelope(request_id: str) -> dict[str, Any]:
    """
    Load a cached orchestrator ingress envelope by request id.

    Args:
        request_id: Ingress request identifier.

    Returns:
        dict[str, Any]: Parsed envelope or empty dict.
    """
    rid = str(request_id or "").strip()
    if not rid:
        return {}
    try:
        raw = _redis.get(f"{_INGRESS_PAYLOAD_CACHE_PREFIX}:{rid}")
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ingress_case_id(request_id: str) -> str:
    """
    Resolve case_id from a cached ingress envelope.

    Args:
        request_id: Ingress request identifier.

    Returns:
        str: Case id or empty string.
    """
    envelope = _load_ingress_envelope(request_id)
    return str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()


def _user_escalation_dedupe_key(client_id: str) -> str:
    """
    Build Redis dedupe key for user-requested specialist escalation.

    Args:
        client_id: Stable client identifier.

    Returns:
        str: Redis key string.
    """
    normalized = str(client_id or "").strip() or "anonymous"
    return _USER_ESCALATION_DEDUPE_KEY_TEMPLATE.format(client_id=normalized)


def resolve_user_escalation_context(
    client_id: str,
    *,
    intake_session: IntakeSession | None = None,
    profile: Mapping[str, Any] | None = None,
    message_id: str = "",
    channel: str = "",
) -> UserEscalationContext | None:
    """
    Build specialist escalation context from persisted skin audit and intake state.

    Requires a persisted skin audit snapshot and/or a registered skin intake image.

    Args:
        client_id: Stable client identifier.
        intake_session: Optional active intake session.
        profile: Optional skin profile mapping.
        message_id: Current-turn message id for relay tracking.
        channel: Current ingress channel.

    Returns:
        UserEscalationContext | None: Resolved context, or None when prerequisites missing.
    """
    normalized_client = str(client_id or "").strip()
    if not normalized_client:
        return None

    completed = load_completed_review_anchor(_redis, normalized_client)
    if completed is None or completed.completed_at_ms <= 0:
        return None

    envelope = load_latest_skin_audit_envelope(normalized_client)
    skin_audit = envelope.get("skin_audit") if isinstance(envelope.get("skin_audit"), dict) else {}
    audit_request_id = completed.anchor_request_id

    session = intake_session
    if session is None:
        try:
            from dermascan.shared.intake_session_store import load_intake_session

            session = load_intake_session(normalized_client, redis_client=_redis)
        except Exception:
            session = None

    profile_map: dict[str, Any] = dict(profile or {})
    skin_ref = None
    product_ref = None
    if session is not None:
        skin_ref = session.images.get(IntakeImageCategory.SKIN.value)
        product_ref = session.images.get(IntakeImageCategory.PRODUCT.value)

    audit_case_id = completed.case_id or _ingress_case_id(audit_request_id)
    if not audit_case_id and session is not None:
        audit_case_id = str(session.case_id or "").strip()

    image_blob_path = completed.image_blob_path or resolve_escalation_image_blob_path(
        _redis,
        request_id=audit_request_id,
        case_id=audit_case_id,
    )
    if not image_blob_path and completed.mode == "diagnose" and skin_ref is not None:
        image_blob_path = resolve_escalation_image_blob_path(
            _redis,
            request_id=skin_ref.request_id,
            case_id=_ingress_case_id(skin_ref.request_id),
        )
    if not image_blob_path and completed.mode == "product_review" and product_ref is not None:
        image_blob_path = resolve_escalation_image_blob_path(
            _redis,
            request_id=product_ref.request_id,
            case_id=_ingress_case_id(product_ref.request_id),
        )

    diagnosis_text = completed.diagnosis_text
    if completed.mode == "diagnose":
        diagnosis_text = (
            diagnosis_text
            or resolve_internal_diagnosis_text(skin_audit)
            or str(skin_audit.get("primary_concern") or "").strip()
            or (str(session.primary_concern).strip() if session is not None else "")
        )
    escalation_context = ""
    if session is not None:
        escalation_context = build_collected_context_summary(session, profile_map).strip()
    if not escalation_context:
        escalation_context = diagnosis_text

    confidence = completed.confidence
    if confidence is None and completed.mode == "diagnose":
        confidence_raw = skin_audit.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None

    summary = completed.summary
    treatment = completed.treatment
    if completed.mode == "diagnose" and not summary:
        current_condition = skin_audit.get("current_condition")
        summary = current_condition if isinstance(current_condition, dict) else {}
    if completed.mode == "diagnose" and not treatment:
        treatment = skin_audit.get("treatment") if isinstance(skin_audit.get("treatment"), dict) else {}

    review_type = completed.review_type or (
        "Product review" if completed.mode == "product_review" else "Skin review"
    )
    resolved_mode = completed.mode if completed.mode in {"diagnose", "product_review", "hybrid"} else "diagnose"

    return UserEscalationContext(
        audit_case_id=audit_case_id,
        audit_request_id=audit_request_id,
        message_id=completed.message_id or str(message_id or "").strip(),
        channel=completed.channel or str(channel or "").strip(),
        image_blob_path=image_blob_path,
        diagnosis_text=diagnosis_text,
        escalation_context=escalation_context,
        risk_tier=completed.risk_tier or str(skin_audit.get("risk_tier") or "cosmetic"),
        summary=summary,
        treatment=treatment,
        confidence=confidence,
        mode=resolved_mode,
        review_type=review_type,
        product_name=completed.product_name,
        description=completed.description,
        indications=completed.indications,
        safety_in_pregnancy=completed.safety_in_pregnancy,
        contraindications=completed.contraindications,
        how_to_use=completed.how_to_use,
        special_instructions=completed.special_instructions,
    )


def dispatch_user_requested_escalation(
    client_id: str,
    *,
    current_request_id: str,
    current_case_id: str,
    message_id: str,
    channel: str,
    intake_session: IntakeSession | None = None,
    profile: Mapping[str, Any] | None = None,
) -> str:
    """
    Queue a specialist escalation job built from persisted client context.

    Used when the user explicitly requests specialist review without a cached
    watch-tier consent offer (for example after a cosmetic-tier skin report).

    Args:
        client_id: Stable client identifier.
        current_request_id: Current orchestrator turn request id.
        current_case_id: Current turn case id (tracking only).
        message_id: Current-turn message id.
        channel: Ingress channel for the current turn.
        intake_session: Optional active intake session.
        profile: Optional skin profile mapping.

    Returns:
        str: ``dispatched``, ``missing_context``, ``already_dispatched``, or ``failed``.
    """
    normalized_client = str(client_id or "").strip()
    if not normalized_client:
        return "failed"

    dedupe_key = _user_escalation_dedupe_key(normalized_client)
    try:
        if _redis.get(dedupe_key):
            return "already_dispatched"
    except Exception:
        pass

    context = resolve_user_escalation_context(
        normalized_client,
        intake_session=intake_session,
        profile=profile,
        message_id=message_id,
        channel=channel,
    )
    if context is None or not context.audit_case_id:
        return "missing_context"

    payload = {
        "request_id": context.audit_request_id or current_request_id,
        "case_id": context.audit_case_id,
        "client_id": normalized_client,
        "message_id": context.message_id or message_id or current_request_id,
        "channel": context.channel or channel,
        "source_agent": "orchestrator",
        "mode": context.mode,
        "reason": "user_requested_specialist",
        "confidence": "" if context.confidence is None else str(context.confidence),
        "threshold": "",
        "diagnosis_text": context.diagnosis_text,
        "requires_human_review": "true",
        "product_name": context.product_name,
        "description": context.description,
        "indications": context.indications,
        "safety_in_pregnancy": context.safety_in_pregnancy,
        "contraindications": context.contraindications,
        "how_to_use": context.how_to_use,
        "special_instructions": context.special_instructions,
        "image_blob_path": context.image_blob_path,
        "review_type": context.review_type,
        "rationale": context.escalation_context,
        "summary": _stream_json(context.summary),
        "treatment": _stream_json(context.treatment),
        "risk_tier": context.risk_tier,
        "user_request_id": current_request_id,
        "user_case_id": current_case_id,
    }
    try:
        _redis.xadd(
            _ESCALATION_STREAM,
            {k: _to_str(v) for k, v in payload.items()},
            maxlen=_EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
        _redis.setex(dedupe_key, _CONSENT_OFFER_TTL_SECONDS, current_request_id or "1")
        client_offer_key = _consent_offer_key_for_client(normalized_client)
        _redis.delete(client_offer_key)
        if context.audit_case_id:
            _redis.delete(_consent_offer_key_legacy_case(context.audit_case_id))
        if context.image_blob_path and context.audit_case_id:
            cache_escalation_image_path(
                _redis,
                case_id=context.audit_case_id,
                blob_path=context.image_blob_path,
            )
    except Exception:
        return "failed"
    return "dispatched"


def _build_orchestrator_signal_payload(
    *,
    escalated: bool,
    mode: str,
    confidence: float | None,
    threshold_used: float,
    base_fields: dict[str, str],
    text: str,
    entry: _JobEntry,
    escalation_offer: bool = False,
    risk_tier: str = "cosmetic",
) -> dict[str, Any]:
    """
    Build orchestrator signal payload.

    Escalated payloads omit model findings so downstream flows cannot leak them to the client.
    Full findings remain on ``escalation:jobs`` and ``events:stream`` for physician review.
    """
    metadata: dict[str, Any] = {
        "extras": {
            "group_id": entry.group_id,
            "group_expected_count": str(entry.group_expected_count),
        }
    }
    if escalated:
        return {
            "mode": mode,
            "escalation": True,
            "confidence": confidence,
            "threshold": threshold_used,
            "metadata": metadata,
        }
    return {
        "review_type": base_fields.get("review_type", ""),
        "diagnosis": resolve_internal_diagnosis_text(base_fields),
        "diagnosis_text": resolve_internal_diagnosis_text(base_fields) or text,
        "rationale": base_fields.get("rationale", ""),
        "summary": base_fields.get("summary", {}),
        "treatment": base_fields.get("treatment", {}),
        "confidence": confidence,
        "threshold": threshold_used,
        "escalation": False,
        "mode": mode,
        "product_name": base_fields.get("product_name", ""),
        "description": base_fields.get("description", ""),
        "indications": base_fields.get("indications", ""),
        "safety_in_pregnancy": base_fields.get("safety_in_pregnancy", ""),
        "contraindications": base_fields.get("contraindications", ""),
        "how_to_use": base_fields.get("how_to_use", ""),
        "special_instructions": base_fields.get("special_instructions", ""),
        "risk_tier": risk_tier,
        "escalation_offer": escalation_offer,
        "metadata": metadata,
    }


def _extract_structured_fields(text: str) -> tuple[dict[str, Any], bool]:
    """
    Parse MedGemma JSON output and extract legacy + cosmetic fields.

    Args:
        text: The JSON text to parse

    Returns:
        A tuple of (fields dictionary, parse_success)
    """
    return extract_structured_fields(text)


def route_case_after_inference(entry: _JobEntry, text: str, confidence: float | None) -> bool:
    """
    Determines whether a case should be escalated to a human expert or NOT.

    Args:
        entry: The job entry containing case information
        text: The JSON text to parse
        confidence: The confidence score of the diagnosis
        
    Returns:
        True if the case should be escalated, False otherwise
    """
    structured, parsed_ok = _extract_structured_fields(text)
    product_fields = (
        "product_name",
        "description",
        "indications",
        "safety_in_pregnancy",
        "contraindications",
        "how_to_use",
        "special_instructions",
    )

    review_type = structured.get("review_type", "").strip().lower()
    diagnosis_present = bool(structured.get("diagnosis")) or bool(structured.get("primary_observation"))
    product_present = any(bool(structured.get(k)) for k in product_fields)

    if review_type in ("product review", "product_review", "product", "hybrid review", "hybrid_review", "hybrid"):
        mode = "product_review"
    elif review_type in ("skin review", "skin_review", "skin", "general review", "general_review", "general"):
        mode = "diagnose"
    elif product_present and diagnosis_present:
        mode = "hybrid"
    elif product_present:
        mode = "product_review"
    elif diagnosis_present:
        mode = "diagnose"
    else:
        # Safe fallback for malformed or empty payloads.
        mode = "diagnose"

    threshold_used = (
        _PRODUCT_REVIEW_CONFIDENCE_THRESHOLD if mode == "product_review" else _DIAGNOSE_CONFIDENCE_THRESHOLD
    )

    base_fields = {
        "schema_version": structured.get("schema_version", ""),
        "review_type": structured["review_type"],
        "rationale": structured["rationale"],
        "primary_observation": structured.get("primary_observation", ""),
        "observation_rationale": structured.get("observation_rationale", ""),
        "product_name": structured["product_name"],
        "description": structured["description"],
        "indications": structured["indications"],
        "safety_in_pregnancy": structured["safety_in_pregnancy"],
        "contraindications": structured["contraindications"],
        "how_to_use": structured["how_to_use"],
        "special_instructions": structured["special_instructions"],
        "diagnosis": structured["diagnosis"],
        "summary": structured["summary"] if isinstance(structured["summary"], dict) else _to_str(structured["summary"]).strip(),
        "skin_state": structured.get("skin_state", structured["summary"])
        if isinstance(structured.get("skin_state"), dict)
        else structured["summary"],
        "treatment": structured["treatment"] if isinstance(structured["treatment"], dict) else _to_str(structured["treatment"]).strip(),
        "cosmetic_implications": structured.get("cosmetic_implications", structured["treatment"])
        if isinstance(structured.get("cosmetic_implications"), dict)
        else structured["treatment"],
        "risk_tier": structured.get("risk_tier", "cosmetic"),
        "image_quality": structured.get("image_quality", "unknown"),
    }

    decision = resolve_consent_escalation_decision(
        mode=mode,
        confidence=confidence,
        threshold=threshold_used,
        parsed_ok=parsed_ok,
        risk_tier=str(base_fields.get("risk_tier") or "cosmetic"),
        consent_escalation_active=cosmetic_consent_escalation_active(),
        base_fields=base_fields,
        audit_context=entry.prompt,
    )
    escalate = decision.auto_escalate
    escalation_offer = decision.offer_escalation
    if decision.reason == "watch_guardrail_inflammatory_untreated":
        base_fields["risk_tier"] = "watch"

    completed_status = f"{mode}_inference_completed"
    escalated_status = f"{mode}_escalated"
    escalation_note = decision.reason
    if not parsed_ok:
        escalation_note = "Model response was not valid JSON"
    elif escalate and decision.reason == "legacy_confidence_threshold":
        escalation_note = (
            "Product review confidence fell below threshold"
            if mode == "product_review"
            else "Diagnosis confidence fell below threshold"
        )

    # push case to EVENT stream - regardless of confidence threshold
    _push_event(
        {
            "request_id": entry.request_id,
            "case_id": entry.case_id,
            "client_id": entry.client_id,
            "message_id": entry.message_id,
            "channel": entry.channel,
            "agent": "medgemma",
            "mode": mode,
            "status": completed_status,
            "confidence": "" if confidence is None else str(confidence),
            "threshold": str(threshold_used),
            "escalation": str(escalate).lower(),
            "escalation_note": escalation_note,
            **base_fields,
        }
    )

    if escalate:

        _push_escalation(entry, text, confidence, base_fields, threshold_used, mode)
        _push_event(
            {
                "request_id": entry.request_id,
                "case_id": entry.case_id,
                "client_id": entry.client_id,
                "message_id": entry.message_id,
                "channel": entry.channel,
                "agent": "medgemma",
                "mode": mode,
                "status": escalated_status,
                "confidence": "" if confidence is None else str(confidence),
                "threshold": str(threshold_used),
                "escalation": "true",
                **base_fields,
            }
        )

        

    _push_signal(
        entry=entry,
        text=text,
        confidence=confidence,
        threshold_used=threshold_used,
        mode=mode,
        escalated=escalate,
        base_fields=base_fields,
        parsed_ok=parsed_ok,
        escalation_offer=escalation_offer,
    )

    if escalation_offer:
        _cache_consent_escalation_offer(
            entry,
            mode=mode,
            confidence=confidence,
            threshold_used=threshold_used,
            base_fields=base_fields,
            text=text,
        )

    try:
        record_completed_review_from_medgemma_fields(
            _redis,
            client_id=str(entry.client_id or "").strip(),
            mode=mode,
            anchor_request_id=str(entry.request_id or "").strip(),
            case_id=str(entry.case_id or "").strip(),
            message_id=str(entry.message_id or "").strip(),
            channel=str(entry.channel or "").strip(),
            image_blob_path=str(entry.image_blob_path or "").strip(),
            base_fields=base_fields,
            diagnosis_text=resolve_internal_diagnosis_text(base_fields) or text,
            confidence=confidence,
        )
    except Exception:
        _LOGGER.exception(
            "failed to record completed review anchor: client_id=%s request_id=%s",
            entry.client_id,
            entry.request_id,
        )

    return escalate


def _route_case_after_inference(entry: _JobEntry, text: str, confidence: float | None) -> bool:
    return route_case_after_inference(entry, text, confidence)


def _push_escalation(
    entry: _JobEntry,
    text: str,
    confidence: float | None,
    base_fields: dict[str, str],
    threshold_used: float,
    mode: str,
) -> None:
    """
    Push a case that falls below the confidence threshold to ESCALATION STREAM.

    Args:
        entry: The job entry
        text: The diagnosis text
        confidence: The confidence score
    """
    _redis.xadd(
        _ESCALATION_STREAM,
        {
            "request_id": entry.request_id,
            "case_id": entry.case_id,
            "client_id": entry.client_id,
            "message_id": entry.message_id,
            "channel": entry.channel,
            "source_agent": "medgemma",
            "mode": mode,
            "reason": "low_confidence",
            "confidence": "" if confidence is None else str(confidence),
            "threshold": str(threshold_used),
            "diagnosis_text": resolve_internal_diagnosis_text(base_fields) or text,
            "requires_human_review": "true",
            "product_name": base_fields.get("product_name", ""),
            "description": base_fields.get("description", ""),
            "indications": base_fields.get("indications", ""),
            "safety_in_pregnancy": base_fields.get("safety_in_pregnancy", ""),
            "contraindications": base_fields.get("contraindications", ""),
            "how_to_use": base_fields.get("how_to_use", ""),
            "special_instructions": base_fields.get("special_instructions", ""),
            "image_blob_path": entry.image_blob_path or "",
            # Carried so the worker builds a full-fidelity {mode}_escalated payload
            # for the physician-return path without a racy events/signal-stream join.
            "review_type": base_fields.get("review_type", ""),
            "rationale": base_fields.get("rationale", ""),
            "summary": _stream_json(base_fields.get("summary")),
            "treatment": _stream_json(base_fields.get("treatment")),
        },
        maxlen=_EVENTS_STREAM_MAXLEN,
        approximate=True,

    )

def _push_signal(
    entry: _JobEntry,
    text: str,
    confidence: float | None,
    threshold_used: float,
    mode: str,
    escalated: bool,
    base_fields: dict[str, str],
    parsed_ok: bool,
    escalation_offer: bool = False,
) -> None:
    """
    Push a completion/escalation signal to ORCHESTRATOR SIGNAL STREAM.

    Args:
        entry: The job entry
        text: The model response text
        confidence: The confidence score
    """
    signal_type = f"{mode}_escalated" if escalated else f"{mode}_completed"
    if not parsed_ok:
        notes = "The model response was not valid JSON"
    elif escalated:
        notes = f"The {mode} inference completed below confidence threshold"
    elif escalation_offer:
        notes = f"The {mode} inference completed with optional specialist review offered"
    else:
        notes = f"The {mode} inference completed above threshold"

    risk_tier = str(base_fields.get("risk_tier") or "cosmetic")

    signal_envelope: dict[str, Any] = {
        "signal_type": signal_type,
        "signal_id": entry.msg_id,
        "request_id": entry.request_id,
        "case_id": entry.case_id,
        "client_id": entry.client_id,
        "message_id": entry.message_id,
        "channel": entry.channel,
        "source_event_id": "",
        "payload": json.dumps(
            _build_orchestrator_signal_payload(
                escalated=escalated,
                mode=mode,
                confidence=confidence,
                threshold_used=threshold_used,
                base_fields=base_fields,
                text=text,
                entry=entry,
                escalation_offer=escalation_offer,
                risk_tier=risk_tier,
            )
        ),
        "notes": notes,
        "confidence": "" if confidence is None else str(confidence),
        "threshold": str(threshold_used),
    }
    if escalated:
        signal_envelope["client_message"] = ESCALATION_CLIENT_MESSAGE
        signal_envelope["suppress_model_output"] = "true"

    signal_fields = enrich_stream_fields(signal_envelope)
    msg_id = str(
        _redis.xadd(
            _ORCHESTRATOR_SIGNAL_STREAM,
            signal_fields,
            maxlen=_EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
    )
    record_stream_produce(
        stream=_ORCHESTRATOR_SIGNAL_STREAM,
        msg_id=msg_id,
        fields=signal_fields,
        extra_metadata={
            "worker": "medgemma_postprocess",
            "signal_type": signal_type,
            "confidence": "" if confidence is None else str(confidence),
            "threshold": str(threshold_used),
        },
    )


def _push_event(fields: dict[str, Any]) -> None:
    """
    Push a case to EVENT STREAM.

    Args:
        fields: The fields to emit
    """
    
    _redis.xadd(
        _EVENTS_STREAM,
        {
            k: _to_str(v) for k, v in fields.items()
        },
        maxlen=_EVENTS_STREAM_MAXLEN,
        approximate=True
    )
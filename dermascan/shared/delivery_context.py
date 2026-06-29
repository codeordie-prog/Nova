"""
Load skin profile and intake worksheet context for audit report delivery.

Signal handlers and report compilers use this module to personalize outbound
reviews with structured intake data collected before enqueue.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field

from dermascan.shared.firebase_storage import initialize_firebase_app
from dermascan.shared.intake_session import (
    IntakeSession,
    build_collected_context_summary,
)
from dermascan.shared.intake_session_store import (
    load_archived_intake_session,
    load_intake_session,
)
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

_LOGGER = get_logger(__name__)

_SKIN_PROFILE_CACHE_PREFIX: str = "skin_profile"
_SKIN_PROFILE_CACHE_TTL_S: int = max(
    60,
    int(os.getenv("SKIN_PROFILE_CACHE_TTL_S", str(60 * 60 * 24 * 30))),
)


class DeliveryContext(BaseModel):
    """
    Profile and intake context for shaping user-facing audit reports.

    Attributes:
        client_id: Normalized client identifier.
        case_id: Case identifier for the active review.
        skin_profile: Persisted skin profile fields (goals, allergies, pregnancy, etc.).
        intake_session: Intake worksheet as JSON-serializable dict when available.
        collected_context_summary: Server-built intake summary for audit enqueue.
        primary_concern: User's stated main concern from intake.
        service_intent: Review service type (skin_review, product_review, combined).
        consultation_notes: Brief notes collected during intake.
    """

    client_id: str = ""
    case_id: str = ""
    skin_profile: dict[str, Any] = Field(default_factory=dict)
    intake_session: dict[str, Any] = Field(default_factory=dict)
    collected_context_summary: str = ""
    primary_concern: str = ""
    service_intent: str = ""
    consultation_notes: list[str] = Field(default_factory=list)


def _skin_profile_cache_key(client_id: str) -> str:
    """
    Build Redis cache key for a client's skin profile.

    Args:
        client_id: Normalized client identifier.

    Returns:
        str: Redis key string.
    """
    normalized = str(client_id or "anonymous").strip() or "anonymous"
    return f"{_SKIN_PROFILE_CACHE_PREFIX}:{normalized}"


def _deserialize_skin_profile(raw: Any) -> dict[str, Any]:
    """
    Parse cached skin profile payload from Redis.

    Args:
        raw: Raw Redis value.

    Returns:
        dict[str, Any]: Profile mapping or empty dict.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def load_skin_profile_dict(
    client_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Load a client's skin profile from Redis cache or Firestore.

    Args:
        client_id: Client identifier.
        redis_client: Optional Redis client override.

    Returns:
        dict[str, Any]: Profile fields, or empty dict when absent.
    """
    cid = str(client_id or "").strip()
    if not cid:
        return {}

    cache_key = _skin_profile_cache_key(cid)
    redis = redis_client
    if redis is not None:
        try:
            cached = run_with_redis_retries(
                lambda: redis.get(cache_key),
                operation_name="delivery_skin_profile_get",
            )
            parsed_cached = _deserialize_skin_profile(cached)
            if parsed_cached:
                return parsed_cached
        except Exception:
            _LOGGER.exception("failed to read skin profile cache: client_id=%s", cid)

    if not os.getenv("REDIS_URL", "").strip() and redis is None:
        try:
            redis = get_redis_client(decode_responses=True)
            cached = run_with_redis_retries(
                lambda: redis.get(cache_key),
                operation_name="delivery_skin_profile_get",
            )
            parsed_cached = _deserialize_skin_profile(cached)
            if parsed_cached:
                return parsed_cached
        except Exception:
            _LOGGER.exception("failed to read skin profile cache: client_id=%s", cid)

    try:
        initialize_firebase_app()
        from google.cloud import firestore  # type: ignore[import-untyped]

        firestore_client = firestore.Client()
        doc = firestore_client.collection("skin_profiles").document(cid).get()
    except Exception:
        _LOGGER.exception("failed to read skin profile from firestore: client_id=%s", cid)
        return {}

    if not doc.exists:
        return {}

    profile = doc.to_dict() or {}
    if not isinstance(profile, dict):
        return {}

    write_redis = redis_client
    if write_redis is None and os.getenv("REDIS_URL", "").strip():
        try:
            write_redis = get_redis_client(decode_responses=True)
        except Exception:
            write_redis = None

    if write_redis is not None and profile:
        try:
            run_with_redis_retries(
                lambda: write_redis.setex(
                    cache_key,
                    _SKIN_PROFILE_CACHE_TTL_S,
                    json.dumps(profile, default=str),
                ),
                operation_name="delivery_skin_profile_set",
            )
        except Exception:
            _LOGGER.exception("failed to cache skin profile: client_id=%s", cid)

    return profile


def _session_from_sources(
    case_id: str,
    client_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> Optional[IntakeSession]:
    """
    Resolve intake session from archive (preferred) or active client key.

    Args:
        case_id: Case identifier.
        client_id: Client identifier.
        redis_client: Optional Redis client override.

    Returns:
        Optional[IntakeSession]: Resolved session or None.
    """
    archived = load_archived_intake_session(case_id, redis_client=redis_client)
    if archived is not None:
        return archived
    return load_intake_session(client_id, redis_client=redis_client)


def load_delivery_context(
    case_id: str,
    client_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> DeliveryContext:
    """
    Load profile and intake context for report personalization.

    Prefers archived intake (post-enqueue) over the active client worksheet.

    Args:
        case_id: Case identifier from the signal or job envelope.
        client_id: Client identifier from the signal or job envelope.
        redis_client: Optional Redis client override.

    Returns:
        DeliveryContext: Context for rules flows and report compilers.
    """
    cid = str(client_id or "anonymous").strip() or "anonymous"
    case = str(case_id or "").strip()
    profile = load_skin_profile_dict(cid, redis_client=redis_client)
    session = _session_from_sources(case, cid, redis_client=redis_client)

    if session is None:
        return DeliveryContext(
            client_id=cid,
            case_id=case,
            skin_profile=profile,
        )

    summary = session.collected_context_summary.strip()
    if not summary:
        summary = build_collected_context_summary(session, profile)

    service_intent = session.service_intent.value if session.service_intent else ""

    return DeliveryContext(
        client_id=cid,
        case_id=case or session.case_id,
        skin_profile=profile,
        intake_session=session.model_dump(mode="json"),
        collected_context_summary=summary,
        primary_concern=session.primary_concern.strip(),
        service_intent=service_intent,
        consultation_notes=list(session.consultation_notes),
    )


def build_rules_skin_context(delivery: DeliveryContext) -> dict[str, Any]:
    """
    Map delivery context into RulesEngine SkinContext fields.

    Args:
        delivery: Loaded delivery context.

    Returns:
        dict[str, Any]: SkinContext-compatible mapping.
    """
    profile = delivery.skin_profile
    concerns: list[str] = []
    if delivery.primary_concern:
        concerns.append(delivery.primary_concern)
    goals = profile.get("current_goals") or []
    if isinstance(goals, list):
        for goal in goals:
            text = str(goal).strip()
            if text and text not in concerns:
                concerns.append(text)

    symptoms: list[str] = []
    conditions = profile.get("current_conditions") or []
    if isinstance(conditions, list):
        symptoms.extend(str(item).strip() for item in conditions if str(item).strip())
    allergies = profile.get("allergies") or []
    if isinstance(allergies, list):
        for allergy in allergies:
            text = str(allergy).strip()
            if text:
                symptoms.append(f"allergy: {text}")

    age_raw = profile.get("age")
    age_range = ""
    if isinstance(age_raw, int) and age_raw > 0:
        age_range = str(age_raw)

    note_parts: list[str] = []
    if delivery.collected_context_summary:
        note_parts.append(delivery.collected_context_summary)
    if delivery.consultation_notes:
        note_parts.append("Notes: " + "; ".join(delivery.consultation_notes))
    profile_notes = str(profile.get("notes") or "").strip()
    if profile_notes:
        note_parts.append(profile_notes)
    if profile.get("pregnancy"):
        note_parts.append("Client is pregnant.")
    if profile.get("breastfeeding"):
        note_parts.append("Client is breastfeeding.")

    return {
        "concerns": concerns[:12],
        "skin_type": str(profile.get("skin_type") or "").strip(),
        "symptoms": symptoms[:12],
        "age_range": age_range,
        "notes": "\n".join(note_parts),
    }


def format_delivery_context_for_prompt(delivery: DeliveryContext) -> str:
    """
    Format delivery context as plain text for LLM report prompts.

    Args:
        delivery: Loaded delivery context.

    Returns:
        str: Multi-line prompt block.
    """
    lines = [
        "Personalize the report using this client context. "
        "Highlight pregnancy/lactation, allergies, goals, and primary concern when relevant.",
        f"service_intent: {delivery.service_intent or 'unknown'}",
        f"primary_concern: {delivery.primary_concern or 'unknown'}",
        f"collected_context_summary: {delivery.collected_context_summary or 'none'}",
        f"skin_profile_json: {json.dumps(delivery.skin_profile, default=str)}",
    ]
    if delivery.consultation_notes:
        lines.append(
            f"consultation_notes: {json.dumps(delivery.consultation_notes, default=str)}"
        )
    return "\n".join(lines)


def merge_profile_into_skin_audit(
    skin_audit: Mapping[str, Any],
    delivery: DeliveryContext,
) -> dict[str, Any]:
    """
    Enrich a persisted skin-audit bridge with intake primary concern when missing.

    Args:
        skin_audit: Existing skin audit mapping (e.g. from Redis).
        delivery: Loaded delivery context.

    Returns:
        dict[str, Any]: Merged skin audit mapping.
    """
    merged = dict(skin_audit)
    if delivery.primary_concern and not str(merged.get("primary_concern") or "").strip():
        merged["primary_concern"] = delivery.primary_concern
    return merged

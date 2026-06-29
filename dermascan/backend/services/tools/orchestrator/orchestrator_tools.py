"""
Author: DermascanTeam
Date: 2026-04-20
Description: Orchestrator tools for the orchestrator Agent

"""

import os
import json
import time
import hashlib
from logging import Logger
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import load_dotenv
from typing import Any, Dict, Optional, Annotated

import httpx
from langchain.tools import tool
from redis import Redis
from tavily import TavilyClient
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries
from dermascan.shared.ingress_payload import (
    payload_has_image_attachment,
    resolve_first_image_url_from_payload,
    resolve_image_urls_from_payload,
)
from dermascan.shared.intake_session import (
    IntakeImageCategory,
    IntakeImageRef,
    IntakePhase,
    IntakeSession,
    MISSING_PRIMARY_CONCERN,
    MISSING_PRODUCT_IMAGE,
    MISSING_PROFILE_MINIMUM,
    MISSING_REPRODUCTIVE_STATUS,
    MISSING_SERVICE_INTENT,
    MISSING_SKIN_IMAGE,
    ServiceIntent,
    build_collected_context_summary,
    is_ready_to_route,
)
from dermascan.shared.intake_session_store import (
    IntakeSessionStoreError,
    archive_intake_session,
    get_or_create_intake_session,
    save_intake_session_recomputed,
)
from dermascan.shared.firebase_storage import initialize_firebase_app

load_dotenv()

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY_QUTIS", "")

# Initialize Logger
logger: Logger = get_logger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
MEDGEMMA_GATEWAY_URL: str = os.getenv("MEDGEMMA_GATEWAY_URL", "http://127.0.0.1:8080")
MEDGEMMA_ENDPOINT_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_ENDPOINT_TIMEOUT_S", "5"))
MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S", "2"))
MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS: int = max(0, int(os.getenv("MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS", "1")))
MEDGEMMA_ENDPOINT_RETRY_DELAY_S: float = max(0.0, float(os.getenv("MEDGEMMA_ENDPOINT_RETRY_DELAY_S", "0.2")))

CASE_STATUS_SCAN_PAGE: int = max(1, int(os.getenv("CASE_STATUS_SCAN_PAGE", "500")))
CASE_STATUS_SCAN_MAX: int = max(CASE_STATUS_SCAN_PAGE, int(os.getenv("CASE_STATUS_SCAN_MAX", "5000")))
CASE_STATUS_HISTORY: int = max(1, int(os.getenv("CASE_STATUS_HISTORY", "5")))
SEARCH_RESULT_CONTENT_CHARS: int = max(200, int(os.getenv("SEARCH_RESULT_CONTENT_CHARS", "700")))
SEARCH_RESULT_TOTAL_CHARS: int = max(1000, int(os.getenv("SEARCH_RESULT_TOTAL_CHARS", "4500")))
TAVILY_TIMEOUT_S: float = max(1.0, float(os.getenv("TAVILY_TIMEOUT_S", "12")))
SEARCH_CACHE_TTL_S: int = max(0, int(os.getenv("SEARCH_CACHE_TTL_S", str(60 * 60 * 6))))
MEDGEMMA_HEALTH_TIMEOUT_S: float = max(0.5, float(os.getenv("MEDGEMMA_HEALTH_TIMEOUT_S", "3")))


def _init_redis_client() -> Redis | None:
    """Build a resilient shared Redis client, failing open when unavailable."""
    if not REDIS_URL:
        return None
    try:
        return get_redis_client(decode_responses=True)
    except Exception:
        logger.exception("failed to initialize resilient redis client")
        return None


_redis: Redis | None = _init_redis_client()
_SOUL_MD_CACHE: str | None = None
_AUDIT_ENQUEUED_KEY_PREFIX: str = "diagnose:enqueued"
_AUDIT_ENQUEUED_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_INGRESS_PAYLOAD_CACHE_PREFIX: str = "orchestrator:ingress_payload"
_INGRESS_PAYLOAD_CACHE_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_SEARCH_CACHE_PREFIX: str = "orchestrator:search_cache"
_SKIN_PROFILE_CACHE_PREFIX: str = "skin_profile"
_SKIN_PROFILE_CACHE_TTL_SEC: int = max(60, int(os.getenv("SKIN_PROFILE_CACHE_TTL_S", str(60 * 60 * 24 * 30))))

class SkinProfile(BaseModel):
    """Canonical skin profile stored for a client."""

    age: int = Field(default=0, ge=0, description="Client age in years.")
    gender: str = Field(
        default="",
        description="Gender or sex relevant to care (e.g. female, male, non-binary).",
    )
    location: str = Field(
        default="",
        description="City, region, or climate context when relevant to skin care.",
    )
    skin_type: str = Field(
        default="",
        description="Self-reported skin type: oily, dry, combination, sensitive, or normal.",
    )
    current_products: list[str] = Field(
        default_factory=list,
        description="Skincare or cosmetic products currently in the client's routine.",
    )
    current_medications: list[str] = Field(
        default_factory=list,
        description="Current medications that may affect skin (topical or systemic).",
    )
    current_conditions: list[str] = Field(
        default_factory=list,
        description="Active or chronic skin or related health conditions.",
    )
    current_goals: list[str] = Field(
        default_factory=list,
        description="Client skincare goals or concerns (e.g. acne, hyperpigmentation, anti-aging).",
    )
    allergies: list[str] = Field(
        default_factory=list,
        description="Known allergies or ingredient sensitivities.",
    )
    pregnancy: bool = Field(default=False, description="Whether the client is currently pregnant.")
    breastfeeding: bool = Field(
        default=False,
        description="Whether the client is currently breastfeeding.",
    )
    notes: str = Field(
        default="",
        description="Free-text intake notes: onset, symptoms, spread, recent changes, or other context.",
    )


class SkinProfileUpdate(BaseModel):
    """
    Partial skin profile update payload for the update_skin_profile tool.

    Only include fields the user stated or you inferred during intake. Omitted fields
    are left unchanged on an existing profile.
    """

    age: Optional[int] = Field(default=None, ge=0, description="Client age in years.")
    gender: Optional[str] = Field(
        default=None,
        description="Gender or sex relevant to care (e.g. female, male, non-binary).",
    )
    location: Optional[str] = Field(
        default=None,
        description="City, region, or climate context when relevant to skin care.",
    )
    skin_type: Optional[str] = Field(
        default=None,
        description="Self-reported skin type: oily, dry, combination, sensitive, or normal.",
    )
    current_products: Optional[list[str]] = Field(
        default=None,
        description="Skincare or cosmetic products currently in the client's routine.",
    )
    current_medications: Optional[list[str]] = Field(
        default=None,
        description="Current medications that may affect skin (topical or systemic).",
    )
    current_conditions: Optional[list[str]] = Field(
        default=None,
        description="Active or chronic skin or related health conditions.",
    )
    current_goals: Optional[list[str]] = Field(
        default=None,
        description="Client skincare goals or concerns (e.g. acne, hyperpigmentation, anti-aging).",
    )
    allergies: Optional[list[str]] = Field(
        default=None,
        description="Known allergies or ingredient sensitivities.",
    )
    pregnancy: Optional[bool] = Field(
        default=None,
        description="Whether the client is currently pregnant.",
    )
    breastfeeding: Optional[bool] = Field(
        default=None,
        description="Whether the client is currently breastfeeding.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-text intake notes: onset, symptoms, spread, recent changes, or other context.",
    )


class UpdateSkinProfileInput(BaseModel):
    """
    Flat tool input for update_skin_profile (OpenAI strict function schemas).

    Nested object parameters are rejected by OpenAI tool calling; all profile
    fields are top-level optional arguments alongside client_id.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        min_length=1,
        description=(
            "Required. Exact client_id from the orchestrator input "
            "(copy the value shown after 'client_id:'). "
            "Do not use request_id or case_id."
        ),
    )
    age: Optional[int] = Field(default=None, ge=0, description="Client age in years.")
    gender: Optional[str] = Field(
        default=None,
        description="Gender or sex relevant to care (e.g. female, male, non-binary).",
    )
    location: Optional[str] = Field(
        default=None,
        description="City, region, or climate context when relevant to skin care.",
    )
    skin_type: Optional[str] = Field(
        default=None,
        description="Self-reported skin type: oily, dry, combination, sensitive, or normal.",
    )
    current_products: Optional[list[str]] = Field(
        default=None,
        description="Skincare or cosmetic products currently in the client's routine.",
    )
    current_medications: Optional[list[str]] = Field(
        default=None,
        description="Current medications that may affect skin (topical or systemic).",
    )
    current_conditions: Optional[list[str]] = Field(
        default=None,
        description="Active or chronic skin or related health conditions.",
    )
    current_goals: Optional[list[str]] = Field(
        default=None,
        description="Client skincare goals or concerns (e.g. acne, hyperpigmentation, anti-aging).",
    )
    allergies: Optional[list[str]] = Field(
        default=None,
        description="Known allergies or ingredient sensitivities.",
    )
    pregnancy: Optional[bool] = Field(
        default=None,
        description="Whether the client is currently pregnant.",
    )
    breastfeeding: Optional[bool] = Field(
        default=None,
        description="Whether the client is currently breastfeeding.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-text intake notes: onset, symptoms, spread, recent changes, or other context.",
    )

    def to_profile_update(self) -> SkinProfileUpdate:
        """
        Build a partial SkinProfileUpdate from set fields only.

        Returns:
            SkinProfileUpdate: Fields explicitly provided on this input.
        """
        return SkinProfileUpdate(**self.model_dump(exclude={"client_id"}, exclude_none=True))


# Tool argument annotations (surfaced to the model in the JSON schema).
OrchestratorRequestIdArg = Annotated[
    str,
    Field(
        description=(
            "Required. Exact request_id from the orchestrator input for this turn "
            "(copy the value shown after 'request_id:' in the message). "
            "Do not use client_id, case_id, or message_id."
        ),
    ),
]

OrchestratorClientIdArg = Annotated[
    str,
    Field(
        description=(
            "Required. Exact client_id from the orchestrator input "
            "(copy the value shown after 'client_id:'). "
            "Do not use request_id or case_id."
        ),
    ),
]

ServiceIntentArg = Annotated[
    ServiceIntent,
    Field(
        description=(
            "Required. User's chosen service. Exactly one of: "
            "skin_review (skin photo review), "
            "product_review (product label/packaging review), "
            "combined (both skin and product), "
            "general (no image audit routing)."
        ),
    ),
]

IntakeImageCategoryArg = Annotated[
    IntakeImageCategory,
    Field(
        description=(
            "Required. Category after visually inspecting the image. "
            "skin = photo of face/body skin; "
            "product = product label, packaging, or bottle."
        ),
    ),
]

AuditTypeArg = Annotated[
    IntakeImageCategory,
    Field(
        description=(
            "Required. Which registered intake image to enqueue: "
            "skin = skin photo audit; product = product label audit. "
            "For combined intent, call enque_request_for_auditing_stream twice "
            "(skin then product) using each slot's request_id from get_intake_status."
        ),
    ),
]

IntakeVisualNoteArg = Annotated[
    str,
    Field(
        default="",
        description=(
            "Optional. Short non-diagnostic intake note about what you see "
            "(e.g. 'SPF label visible', 'rash on cheek'). Not a diagnosis."
        ),
    ),
]

IntakeAttachmentIdArg = Annotated[
    str,
    Field(
        default="",
        description=(
            "Optional. attachment_id from ingress_images_for_this_message when the user "
            "sent multiple images in one message. Omit when only one image is present."
        ),
    ),
]

PrimaryConcernArg = Annotated[
    str,
    Field(
        description=(
            "Required. User's main question or goal in one short sentence "
            "(e.g. 'Is this moisturizer safe during pregnancy?')."
        ),
    ),
]

IntakeNoteArg = Annotated[
    str,
    Field(
        description=(
            "Required. One short consultation fact from the user "
            "(symptom, timeline, product change). Not the main question."
        ),
    ),
]

AuditCollectedContextArg = Annotated[
    str,
    Field(
        default="",
        description=(
            "Optional supplement to the server-built intake summary. "
            "Short facts only; do not paste raw chat. Leave empty to use intake session summary."
        ),
    ),
]


def _normalize_client_id(client_id: str) -> str:
    """
    Normalize user-provided client id into a safe cache/document key.
    
    Args:
        client_id (str): The exact client id provided

    Returns:
        str - The normalized client id
    """
    return str(client_id or "").strip()


def _skin_profile_cache_key(client_id: str) -> str:
    """
    Build Redis key for a client's skin profile.

    Args:
        client_id (str): The exact client id provided

    Returns:
        str - The Redis key
    """
    return f"{_SKIN_PROFILE_CACHE_PREFIX}:{client_id}"


def _serialize_skin_profile(profile: dict[str, Any]) -> str:
    """
    Serialize a skin profile for Redis storage.

    Args:
        profile (dict[str, Any]): The skin profile to serialize

    Returns:
        str - The serialized skin profile
    """
    return json.dumps(profile, default=str)


def _deserialize_skin_profile(raw_profile: Any) -> dict[str, Any]:
    """
    Deserialize a skin profile value from Redis payload to dict.

    Args:
        raw_profile (Any): The raw profile to deserialize

    Returns:
        dict[str, Any] - The deserialized skin profile
    """
    if isinstance(raw_profile, dict):
        return raw_profile
    if isinstance(raw_profile, str):
        try:
            parsed = json.loads(raw_profile)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _fetch_skin_profile_dict(cid: str) -> dict[str, Any]:
    """
    Load a client's skin profile from Redis cache or Firestore.

    Args:
        cid: Normalized client id.

    Returns:
        dict[str, Any]: Profile fields, or empty dict when absent.
    """
    if not cid:
        return {}

    cache_key = _skin_profile_cache_key(cid)
    if _redis is not None:
        try:
            cached_profile = run_with_redis_retries(
                lambda: _redis.get(cache_key),
                operation_name="skin_profile_get",
            )
            parsed_cached = _deserialize_skin_profile(cached_profile)
            if parsed_cached:
                return parsed_cached
        except Exception:
            logger.exception("failed to read skin profile cache: client_id=%s", cid)

    firestore_client = _get_firestore_client()
    if firestore_client is None:
        return {}

    try:
        doc = firestore_client.collection("skin_profiles").document(cid).get()
    except Exception:
        logger.exception("failed to read skin profile from firestore: client_id=%s", cid)
        return {}

    if not doc.exists:
        return {}

    skin_profile = doc.to_dict() or {}
    if not isinstance(skin_profile, dict):
        return {}

    if _redis is not None and skin_profile:
        try:
            run_with_redis_retries(
                lambda: _redis.setex(
                    cache_key,
                    _SKIN_PROFILE_CACHE_TTL_SEC,
                    _serialize_skin_profile(skin_profile),
                ),
                operation_name="skin_profile_set",
            )
        except Exception:
            logger.exception("failed to cache skin profile from firestore: client_id=%s", cid)
    return skin_profile


def _merge_skin_profile(
    existing: dict[str, Any],
    updates: SkinProfileUpdate,
) -> dict[str, Any]:
    """
    Merge partial profile updates onto defaults and any stored profile.

    Args:
        existing: Current stored profile fields.
        updates: Partial update from the orchestrator tool call.

    Returns:
        dict[str, Any]: Validated full profile ready for persistence.

    Raises:
        ValueError: When merged fields fail SkinProfile validation.
    """
    base = SkinProfile().model_dump()
    patch = updates.model_dump(exclude_none=True)
    merged = {**base, **existing, **patch}
    return SkinProfile.model_validate(merged).model_dump()


def _get_firestore_client() -> Any | None:
    """
    Best-effort Firestore client init; fails open to keep tool flow resilient.

    Returns:
        Any | None - The Firestore client
    """
    try:
        initialize_firebase_app()
        from firebase_admin import firestore

        return firestore.client()
    except Exception:
        logger.exception("failed to initialize firestore client")
        return None
    

def _payload_cache_key(request_id: str) -> str:
    """
    Build Redis key for request-scoped ingress payload cache.

    Args:
        request_id (str): The request id

    Returns:
        str - The Redis key
    """
    return f"{_INGRESS_PAYLOAD_CACHE_PREFIX}:{request_id}"


def cache_ingress_payload(
    *,
    request_id: str,
    payload: str,
    case_id: str = "",
    client_id: str = "",
    message_id: str = "",
    channel: str = "",
) -> None:
    """
    Cache the ingress envelope by request id for deterministic tool calls.

    Args:
        request_id: Request identifier.
        payload: Canonical payload JSON string.
        case_id: Case identifier.
        client_id: End-user/client identifier.
        message_id: Source message identifier.
        channel: Source channel name.
    """
    if _redis is None:
        return
    rid = str(request_id or "").strip()
    if not rid:
        return
    try:
        cached_envelope = {
            "request_id": rid,
            "case_id": str(case_id or rid),
            "client_id": str(client_id or "anonymous"),
            "message_id": str(message_id or rid),
            "channel": str(channel or "whatsapp"),
            "payload": str(payload or "{}"),
        }
        _redis.setex(
            _payload_cache_key(rid),
            _INGRESS_PAYLOAD_CACHE_TTL_SEC,
            json.dumps(cached_envelope, default=str),
        )
    except Exception:
        logger.exception("failed to cache ingress envelope: request_id=%s", rid)


def _get_cached_ingress_payload(request_id: str) -> str:
    """
    Get cached ingress payload for request id.
    
    Args:
        request_id (str): The request id

    Returns:
        Cached payload JSON string or empty string.
    """
    if _redis is None:
        return ""
    rid = str(request_id or "").strip()
    if not rid:
        return ""
    try:
        return str(_redis.get(_payload_cache_key(rid)) or "")
    except Exception:
        logger.exception("failed to read ingress payload cache: request_id=%s", rid)
        return ""


def _stringify_cached_payload(payload: Any) -> str:
    """
    Convert a cached payload value into canonical JSON text.

    Args:
        payload: Cached payload as a JSON string or mapping.

    Returns:
        JSON string payload, or `{}` when no payload is available.
    """
    if isinstance(payload, str):
        return payload or "{}"
    if isinstance(payload, dict):
        return json.dumps(payload, default=str)
    return "{}"


def _get_cached_ingress_envelope(request_id: str) -> dict[str, str]:
    """
    Read cached ingress envelope fields needed for audit dispatch.

    Args:
        request_id: Request id from the current orchestrator prompt.

    Returns:
        Dict containing request_id, case_id, client_id, message_id, channel,
        and payload. Empty dict when the cache is unavailable.
    """
    rid = str(request_id or "").strip()
    cached = _get_cached_ingress_payload(rid)
    if not cached:
        return {}

    try:
        cached_obj: Any = json.loads(cached)
    except Exception:
        logger.exception("cached ingress envelope malformed: request_id=%s", rid)
        return {}

    if not isinstance(cached_obj, dict):
        return {}

    if "payload" in cached_obj:
        return {
            "request_id": str(cached_obj.get("request_id") or rid),
            "case_id": str(cached_obj.get("case_id") or rid),
            "client_id": str(cached_obj.get("client_id") or "anonymous"),
            "message_id": str(cached_obj.get("message_id") or rid),
            "channel": str(cached_obj.get("channel") or "whatsapp"),
            "payload": _stringify_cached_payload(cached_obj.get("payload")),
        }

    # Backward compatibility for old cache entries that stored only the payload.
    return {
        "request_id": rid,
        "case_id": rid,
        "client_id": "anonymous",
        "message_id": rid,
        "channel": "whatsapp",
        "payload": json.dumps(cached_obj, default=str),
    }


def _ingress_error(request_id: str, detail: str) -> dict[str, Any]:
    """
    Build a standard ingress resolution error payload for tool responses.

    Args:
        request_id (str): The request id
        detail (str): The detail of the error

    Returns:
        dict[str, Any] - The error payload
    """
    return {
        "status": "error",
        "detail": detail,
        "request_id": str(request_id or "").strip(),
    }


def _require_ingress_envelope(request_id: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """
    Load cached ingress envelope or return an agent-facing error payload.

    Args:
        request_id (str): The request id

    Returns:
        tuple[dict[str, str] | None, dict[str, Any] | None] - The envelope and error
    """
    envelope = _get_cached_ingress_envelope(request_id)
    if envelope:
        return envelope, None
    return None, _ingress_error(
        request_id,
        "ingress cache not found for request_id. Use the exact request_id from the current turn.",
    )


def _load_skin_profile_mapping(client_id: str) -> dict[str, Any]:
    """
    Load skin profile fields for intake recomputation.

    Args:
        client_id (str): The client id

    Returns:
        dict[str, Any] - The skin profile
    """
    cid = _normalize_client_id(client_id)
    profile = _fetch_skin_profile_dict(cid)
    if profile:
        return profile
    return SkinProfile().model_dump()


def _persist_intake_session(
    session: IntakeSession,
    profile: dict[str, Any],
) -> tuple[IntakeSession | None, dict[str, Any] | None]:
    """
    Recompute and save an intake session after a tool mutation.

    Args:
        session (IntakeSession): The intake session
        profile (dict[str, Any]): The skin profile

    Returns:
        tuple[IntakeSession | None, dict[str, Any] | None] - The updated session and error
    """
    try:
        updated = save_intake_session_recomputed(session, profile, redis_client=_redis)
        return updated, None
    except IntakeSessionStoreError as exc:
        logger.exception("failed to persist intake session: client_id=%s", session.client_id)
        return None, {
            "status": "error",
            "detail": f"failed to persist intake session: {exc}",
            "client_id": session.client_id,
            "case_id": session.case_id,
        }
    except Exception as exc:
        logger.exception("unexpected intake session persistence failure: client_id=%s", session.client_id)
        return None, {
            "status": "error",
            "detail": f"failed to persist intake session: {exc}",
            "client_id": session.client_id,
            "case_id": session.case_id,
        }


def _archive_intake_for_signal_delivery(session: IntakeSession) -> None:
    """
    Archive intake worksheet for signal delivery without clearing the active key.

    Args:
        session (IntakeSession): Session to archive (typically awaiting_audit).
    """
    try:
        archive_intake_session(session, redis_client=_redis, delete_active=False)
    except IntakeSessionStoreError:
        logger.exception(
            "failed to archive intake for signal delivery: case_id=%s client_id=%s",
            session.case_id,
            session.client_id,
        )


def _mark_intake_awaiting_audit(
    session: IntakeSession,
    profile: dict[str, Any],
) -> None:
    """
    Set intake phase to awaiting_audit, persist, and archive for signal handlers.

    Args:
        session (IntakeSession): Current intake session.
        profile (dict[str, Any]): Skin profile for recomputation.
    """
    awaiting_session = session.model_copy(update={"phase": IntakePhase.AWAITING_AUDIT})
    updated, _ = _persist_intake_session(awaiting_session, profile)
    if updated is not None:
        _archive_intake_for_signal_delivery(updated)


def _intake_stop_suffix() -> str:
    """
    Standard instruction suffix to prevent tool-call loops within one turn.

    Returns:
        str: Anti-loop guidance for tool responses.
    """
    return (
        " Then compose your final reply to the user in this turn. "
        "Do NOT call get_intake_status again this turn. "
        "Do NOT repeat a tool that already returned saved/registered/queued."
    )


def _derive_intake_next_action(session: IntakeSession, profile: dict[str, Any]) -> str:
    """
    Recommend exactly one next tool for the orchestrator model.

    Args:
        session (IntakeSession): Current intake session.
        profile (dict[str, Any]): Skin profile mapping.

    Returns:
        str: Tool name the model should call next, or reply_only when done.
    """
    if is_ready_to_route(session, profile):
        return "enque_request_for_auditing_stream"

    missing = session.missing_for_routing
    if MISSING_SERVICE_INTENT in missing:
        return "set_service_intent"
    if MISSING_SKIN_IMAGE in missing or MISSING_PRODUCT_IMAGE in missing:
        return "register_intake_image"
    if MISSING_PRIMARY_CONCERN in missing:
        return "set_primary_concern"
    if MISSING_PROFILE_MINIMUM in missing:
        return "update_skin_profile"
    if MISSING_REPRODUCTIVE_STATUS in missing:
        return "confirm_reproductive_status"
    return "reply_only"


def _intake_instruction(session: IntakeSession, profile: dict[str, Any]) -> str:
    """
    Build a short next-step instruction for the agent from intake state.

    Args:
        session (IntakeSession): The intake session
        profile (dict[str, Any]): Skin profile for routing checks

    Returns:
        str - The instruction
    """
    stop = _intake_stop_suffix()
    next_tool = _derive_intake_next_action(session, profile)

    if session.phase == IntakePhase.AWAITING_AUDIT:
        return (
            "Audits are already queued. Tell the user their photo is being reviewed. "
            "Do NOT call enque_request_for_auditing_stream or get_intake_status."
        )

    if is_ready_to_route(session, profile):
        if session.service_intent == ServiceIntent.COMBINED:
            return (
                "Call enque_request_for_auditing_stream twice: audit_type=skin then product "
                f"(current turn request_id).{stop}"
            )
        audit_type = "skin"
        if session.service_intent == ServiceIntent.PRODUCT_REVIEW:
            audit_type = "product"
        return (
            f"Call enque_request_for_auditing_stream once with audit_type={audit_type} "
            f"(current turn request_id).{stop}"
        )

    if MISSING_PROFILE_MINIMUM in session.missing_for_routing:
        gaps = ", ".join(session.profile_gaps) or "profile fields from profile_gaps"
        return (
            f"Call update_skin_profile with client_id and: {gaps}. "
            f"Use values the user stated in their latest message.{stop}"
        )

    if MISSING_REPRODUCTIVE_STATUS in session.missing_for_routing:
        return (
            "Set pregnancy/breastfeeding via update_skin_profile if the user answered, "
            f"then call confirm_reproductive_status once.{stop}"
        )

    if session.missing_for_routing:
        missing = ", ".join(session.missing_for_routing)
        return (
            f"Missing: {missing}. Call {next_tool} once using the latest user message.{stop}"
        )

    return f"Continue intake. Next tool: {next_tool}.{stop}"


def _format_intake_status(
    session: IntakeSession,
    profile: dict[str, Any],
    *,
    request_id: str = "",
) -> dict[str, Any]:
    """
    Format intake session state for agent tool responses.
    
    Args:
        session (IntakeSession): The intake session
        profile (dict[str, Any]): The skin profile
        request_id (str): The request id

    Returns:
        dict[str, Any] - The intake status
    """
    skin_ref = session.images.get(IntakeImageCategory.SKIN.value)
    product_ref = session.images.get(IntakeImageCategory.PRODUCT.value)
    ready = is_ready_to_route(session, profile)

    return {
        "status": "ok",
        "request_id": request_id,
        "phase": session.phase.value,
        "service_intent": session.service_intent.value if session.service_intent else None,
        "primary_concern": session.primary_concern,
        "reproductive_status_confirmed": session.reproductive_status_confirmed,
        "images": {
            "skin": {
                "present": skin_ref is not None,
                "request_id": skin_ref.request_id if skin_ref else "",
            },
            "product": {
                "present": product_ref is not None,
                "request_id": product_ref.request_id if product_ref else "",
            },
        },
        "profile_gaps": list(session.profile_gaps),
        "missing_for_routing": list(session.missing_for_routing),
        "ready_to_route": ready,
        "collected_context_summary": session.collected_context_summary,
        "next_recommended_tool": _derive_intake_next_action(session, profile),
        "instruction": _intake_instruction(session, profile),
    }


def _resolve_registration_image(
    envelope: dict[str, str],
    attachment_id: Optional[str] = None,
) -> tuple[str, str, str] | None:
    """
    Resolve one image URL from an ingress envelope for intake registration.

    Args:
        envelope (dict[str, str]): The ingress envelope
        attachment_id (Optional[str]): The attachment id

    Returns:
        tuple[str, str, str] | None - The image url
    """
    try:
        payload_obj: dict[str, Any] = json.loads(str(envelope.get("payload") or "{}"))
    except Exception:
        payload_obj = {}

    resolved_images = resolve_image_urls_from_payload(payload_obj, redis_client=_redis)
    if not resolved_images:
        return None

    target_attachment = str(attachment_id or "").strip()
    if target_attachment:
        for image in resolved_images:
            if image.attachment_id == target_attachment:
                return (
                    str(envelope.get("request_id") or "").strip(),
                    image.attachment_id,
                    image.image_url,
                )
        return None

    first = resolved_images[0]
    return (
        str(envelope.get("request_id") or "").strip(),
        first.attachment_id,
        first.image_url,
    )


def _resolve_canonical_payload(*, request_id: str, payload: str) -> str:
    """
    Ensure payload is valid JSON; recover from cache when malformed.

    Args:
        request_id: Request id used for cache fallback.
        payload: Payload provided by tool call.

    Returns:
        Valid payload JSON string.
    """
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return json.dumps(parsed, default=str)
    except Exception:
        logger.warning("tool payload json invalid; attempting cache fallback: request_id=%s", request_id)

    envelope = _get_cached_ingress_envelope(request_id)
    if envelope:
        try:
            parsed = json.loads(envelope["payload"])
            if isinstance(parsed, dict):
                return json.dumps(parsed, default=str)
        except Exception:
            logger.exception("cached ingress payload malformed: request_id=%s", request_id)
    return payload


def _merge_collected_context_into_payload(payload: str, collected_context: str) -> str:
    """
    Merge orchestrator-collected context into payload.content.text for audit.

    Args:
        payload: Canonical payload JSON string.
        collected_context: Structured intake summary prepared by orchestrator.

    Returns:
        Updated payload JSON string. Returns original payload on parse failures.
    """
    context_text = str(collected_context or "").strip()
    if not context_text:
        return payload

    try:
        payload_obj: dict[str, Any] = json.loads(payload)
    except Exception:
        logger.exception("failed to parse payload while merging collected_context")
        return payload

    content_obj = payload_obj.get("content")
    if not isinstance(content_obj, dict):
        content_obj = {}
        payload_obj["content"] = content_obj
    existing_text = str(content_obj.get("text") or "").strip()
    content_obj["text"] = (
        f"{existing_text}\n\nCollected context:\n{context_text}".strip()
        if existing_text
        else f"Collected context:\n{context_text}"
    )

    metadata_obj = payload_obj.get("metadata")
    if not isinstance(metadata_obj, dict):
        metadata_obj = {}
        payload_obj["metadata"] = metadata_obj
    extras_obj = metadata_obj.get("extras")
    if not isinstance(extras_obj, dict):
        extras_obj = {}
        metadata_obj["extras"] = extras_obj
    extras_obj["audit_context_enriched"] = "true"
    # Backward compatibility for any downstream readers that still inspect this marker.
    extras_obj["diagnose_prompt_enriched"] = "true"

    return json.dumps(payload_obj, default=str)


def _resolve_image_url_from_payload(payload_obj: dict[str, Any]) -> str:
    """
    Resolve a downloadable image URL from canonical payload attachments.

    Args:
        payload_obj: Canonical payload object.

    Returns:
        Image URL string, or empty string when not resolvable.
    """
    return resolve_first_image_url_from_payload(payload_obj, redis_client=_redis)


def _extract_prompt_context(payload_obj: dict[str, Any]) -> str:
    """
    Build the context string sent to MedGemma from canonical payload content.

    Args:
        payload_obj: Canonical payload object.

    Returns:
        Context text (possibly empty).
    """
    content_obj = payload_obj.get("content") or {}
    if not isinstance(content_obj, dict):
        return ""
    return str(content_obj.get("text") or "").strip()


def _emit_orchestrator_event(fields: dict[str, str]) -> None:
    """
    Best-effort append to orchestrator events stream.

    Args:
        fields: Event fields to append.
    """
    if _redis is None:
        return
    try:
        _redis.xadd(
            EVENTS_STREAM,
            {k: ("" if v is None else str(v)) for k, v in fields.items()},
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("failed to append orchestrator event: status=%s", fields.get("status"))


def _parse_retry_after_seconds(value: str) -> str:
    """
    Parse Retry-After header into a user-friendly numeric-seconds string.

    Args:
        value: Raw Retry-After header value.

    Returns:
        Parsed integer seconds as string, or empty string when parsing fails.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        seconds = max(1, int(float(raw)))
        return str(seconds)
    except (TypeError, ValueError):
        return ""


def _audit_enqueued_dedupe_key(request_id: str) -> str:
    """
    Build Redis dedupe key for a dispatched image audit.

    Args:
        request_id (str): Ingress request id of the image message.

    Returns:
        str: Redis key string.
    """
    return f"{_AUDIT_ENQUEUED_KEY_PREFIX}:{request_id}"


def _is_audit_enqueued(request_id: str) -> bool:
    """
    Return True when an audit dispatch dedupe marker exists for request_id.

    Args:
        request_id (str): Ingress request id of the image message.

    Returns:
        bool: True when already enqueued.
    """
    if _redis is None:
        return False
    try:
        return bool(_redis.get(_audit_enqueued_dedupe_key(request_id)))
    except Exception:
        logger.exception("failed to read audit dedupe key: request_id=%s", request_id)
        return False


def _required_audit_categories(session: IntakeSession) -> list[IntakeImageCategory]:
    """
    List image categories that must be enqueued for the session service intent.

    Args:
        session (IntakeSession): Current intake session.

    Returns:
        list[IntakeImageCategory]: Required audit categories.
    """
    intent = session.service_intent
    if intent == ServiceIntent.SKIN_REVIEW:
        return [IntakeImageCategory.SKIN]
    if intent == ServiceIntent.PRODUCT_REVIEW:
        return [IntakeImageCategory.PRODUCT]
    if intent == ServiceIntent.COMBINED:
        return [IntakeImageCategory.SKIN, IntakeImageCategory.PRODUCT]
    return []


def _all_required_audits_enqueued(session: IntakeSession) -> bool:
    """
    Return True when every required audit category has a dedupe marker in Redis.

    Args:
        session (IntakeSession): Current intake session.

    Returns:
        bool: True when all required image audits were dispatched.
    """
    required = _required_audit_categories(session)
    if not required:
        return False
    for category in required:
        ref = session.images.get(category.value)
        if ref is None or not _is_audit_enqueued(ref.request_id):
            return False
    return True


def _audit_type_allowed(session: IntakeSession, audit_type: IntakeImageCategory) -> bool:
    """
    Return True when audit_type matches the session service intent.

    Args:
        session (IntakeSession): Current intake session.
        audit_type (IntakeImageCategory): Requested audit category.

    Returns:
        bool: True when enqueue is allowed for this audit type.
    """
    intent = session.service_intent
    if intent == ServiceIntent.SKIN_REVIEW:
        return audit_type == IntakeImageCategory.SKIN
    if intent == ServiceIntent.PRODUCT_REVIEW:
        return audit_type == IntakeImageCategory.PRODUCT
    if intent == ServiceIntent.COMBINED:
        return audit_type in (IntakeImageCategory.SKIN, IntakeImageCategory.PRODUCT)
    return False


def _enqueue_group_params(session: IntakeSession) -> tuple[str, int]:
    """
    Derive grouped-media parameters for combined skin+product enqueue.

    Args:
        session (IntakeSession): Current intake session.

    Returns:
        tuple[str, int]: group_id and group_expected_count for MedGemma dispatch.
    """
    if session.service_intent == ServiceIntent.COMBINED:
        return session.case_id, 2
    return "", 0


def _build_audit_context_text(
    session: IntakeSession,
    profile: dict[str, Any],
    audit_type: IntakeImageCategory,
    collected_context: str,
) -> str:
    """
    Build MedGemma audit context from intake summary plus audit focus.

    Args:
        session (IntakeSession): Current intake session.
        profile (dict[str, Any]): Skin profile mapping.
        audit_type (IntakeImageCategory): Audit category being enqueued.
        collected_context (str): Optional model supplement.

    Returns:
        str: Context string for audit dispatch.
    """
    base_summary = session.collected_context_summary.strip()
    if not base_summary:
        base_summary = build_collected_context_summary(session, profile)

    focus = (
        "Audit focus: skin image review (dermatological assessment)."
        if audit_type == IntakeImageCategory.SKIN
        else "Audit focus: product image review (label and ingredient assessment)."
    )
    parts = [base_summary, focus]
    extra = str(collected_context or "").strip()
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _enqueue_audit_instruction(
    session: IntakeSession,
    audit_type: IntakeImageCategory,
    dispatch_status: str,
) -> str:
    """
    Build agent instruction after a terminal enqueue outcome.

    Args:
        session (IntakeSession): Current intake session.
        audit_type (IntakeImageCategory): Audit category just enqueued.
        dispatch_status (str): Dispatch result status.

    Returns:
        str: Instruction for the orchestrator model.
    """
    if dispatch_status not in {"queued", "already_queued"}:
        return ""

    if session.service_intent == ServiceIntent.COMBINED:
        other = (
            IntakeImageCategory.PRODUCT
            if audit_type == IntakeImageCategory.SKIN
            else IntakeImageCategory.SKIN
        )
        other_ref = session.images.get(other.value)
        if other_ref and not _is_audit_enqueued(other_ref.request_id):
            return (
                f"The {audit_type.value} audit is queued. Call enque_request_for_auditing_stream "
                f"again with audit_type={other.value}. Do NOT re-enqueue {audit_type.value}. "
                "Tell the user both photos are being reviewed when both are queued."
            )

    return (
        "The image audit request is now in the queue. Do NOT call this tool again. "
        "Tell the user their photo was received and is being reviewed by the specialist team, "
        "and that results will follow shortly."
    )


def _dispatch_audit_image_ref(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    image_url: str,
    context: str,
    group_id: str = "",
    group_expected_count: int = 0,
) -> dict[str, str]:
    """
    Dispatch directly to MedGemma ref endpoint.

    Args:
        request_id: Request identifier.
        case_id: Case identifier.
        client_id: Client identifier.
        message_id: Message identifier.
        channel: Source channel.
        image_url: Signed/downloadable image URL.
        context: Model context text.
        group_id: Group identifier.
        group_expected_count: Group expected count.

    Returns:
        Status dictionary for tool caller.
    """
    endpoint = f"{MEDGEMMA_GATEWAY_URL.rstrip('/')}/audit_image_ref"
    payload: dict[str, Any] = {
        "request_id": request_id,
        "case_id": case_id,
        "client_id": client_id,
        "message_id": message_id,
        "channel": channel,
        "image_url": image_url,
        "context": context,
    }
    if group_id:
        payload["group_id"] = group_id
    if group_expected_count > 0:
        payload["group_expected_count"] = group_expected_count

    timeout = httpx.Timeout(
        timeout=MEDGEMMA_ENDPOINT_TIMEOUT_S,
        connect=MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S,
    )
    response: httpx.Response | None = None

    for attempt in range(MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS + 1):
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                timeout=timeout,
            )
            break
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
            if attempt >= MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS:
                raise
            time.sleep(MEDGEMMA_ENDPOINT_RETRY_DELAY_S * (attempt + 1))

    if response is None:
        raise RuntimeError("audit dispatch produced no HTTP response")

    if response.status_code == 429:
        retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After", ""))
        return {
            "status": "under_pressure",
            "request_id": request_id,
            "endpoint": endpoint,
            "retry_after_seconds": retry_after or "1",
            "detail": "MedGemma queue is saturated; retry later.",
            "instruction": (
                "MedGemma is currently under pressure. Do NOT call this tool again right now. "
                "Tell the user their request was received but processing is delayed."
            ),
        }

    response.raise_for_status()
    data: dict[str, Any] = response.json() if response.content else {}
    return {
        "status": str(data.get("status") or "queued"),
        "request_id": request_id,
        "endpoint": endpoint,
        "medgemma_job_id": str(data.get("job_id") or ""),
    }


@tool
def get_time() -> str:
    """
    Useful when you need to know the current time
    """
    logger.info(f"TOOL CALL: get_time")
    return datetime.now(timezone.utc).isoformat()


@tool
def get_intake_status(request_id: OrchestratorRequestIdArg) -> dict[str, Any]:
    """
    Read the intake worksheet: phase, service intent, saved skin/product images,
    profile gaps, missing items, and whether routing is allowed.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.

    Returns:
        JSON with status, phase, service_intent, images.skin/product (present + request_id),
        profile_gaps, missing_for_routing, ready_to_route, collected_context_summary, instruction.

    Call after any intake change and always before enque_request_for_auditing_stream.
    """
    logger.info("TOOL CALL: get_intake_status: request_id=%s", request_id)

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {
            "status": "error",
            "detail": f"failed to load intake session: {exc}",
            "request_id": request_id,
        }

    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    return _format_intake_status(updated, profile, request_id=request_id)


@tool
def set_service_intent(
    request_id: OrchestratorRequestIdArg,
    intent: ServiceIntentArg,
) -> dict[str, Any]:
    """
    Record which review service the user wants.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.
        intent (enum, required): Exactly one of:
            - skin_review: user wants a skin image reviewed.
            - product_review: user wants a product label/packaging reviewed.
            - combined: user wants both skin and product reviewed.
            - general: general enquiry; no image audit routing.

    Returns:
        Updated intake status JSON (same shape as get_intake_status) with status saved.

    Call when the user picks a menu option or states their goal. Then call get_intake_status.
    """
    logger.info("TOOL CALL: set_service_intent: request_id=%s intent=%s", request_id, intent)

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    session = session.model_copy(update={"service_intent": intent})
    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "saved"
    return result


@tool
def register_intake_image(
    request_id: OrchestratorRequestIdArg,
    image_category: IntakeImageCategoryArg,
    visual_note: IntakeVisualNoteArg = "",
    attachment_id: IntakeAttachmentIdArg = "",
) -> dict[str, Any]:
    """
    Register a categorized image after you have inspected it in the message.

    Arguments:
        request_id (string, required): request_id of the message that contained the image
            (from ingress_images_for_this_message, not necessarily the current turn if
            the image was sent earlier).
        image_category (enum, required): skin or product (see schema).
        visual_note (string, optional): Brief non-diagnostic observation; default empty.
        attachment_id (string, optional): From ingress_images_for_this_message when
            multiple images were sent together; default empty.

    Do not pass image URLs — the server resolves them from the cached ingress payload.

    Returns:
        Intake status JSON plus status registered, registered_category, registered_image_request_id.

    Call once per image after visually classifying it. Then call get_intake_status.
    """
    logger.info(
        "TOOL CALL: register_intake_image: request_id=%s category=%s attachment_id=%s",
        request_id,
        image_category,
        attachment_id,
    )

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    try:
        payload_obj: dict[str, Any] = json.loads(str(envelope.get("payload") or "{}"))
    except Exception:
        payload_obj = {}

    if not payload_has_image_attachment(payload_obj):
        return {
            "status": "blocked_missing_image",
            "request_id": str(envelope.get("request_id") or request_id),
            "instruction": (
                "This request_id has no image attachment. Do NOT call this tool again for it. "
                "Ask the user to send a clear photo, then register using that message's request_id."
            ),
        }

    resolved = _resolve_registration_image(envelope, attachment_id=attachment_id or None)
    if resolved is None:
        return {
            "status": "error",
            "request_id": str(envelope.get("request_id") or request_id),
            "detail": "could not resolve a downloadable image URL for this request",
            "instruction": (
                "The image could not be resolved for storage. Ask the user to resend the photo."
            ),
        }

    image_request_id, resolved_attachment_id, image_url = resolved
    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or image_request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    images = dict(session.images)
    images[image_category.value] = IntakeImageRef(
        request_id=image_request_id,
        image_url=image_url,
        attachment_id=resolved_attachment_id,
        registered_at_ms=int(time.time() * 1000),
        visual_note=str(visual_note or "").strip(),
    )
    session = session.model_copy(update={"images": images})

    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "registered"
    result["registered_category"] = image_category.value
    result["registered_image_request_id"] = image_request_id
    return result


@tool
def set_primary_concern(
    request_id: OrchestratorRequestIdArg,
    concern: PrimaryConcernArg,
) -> dict[str, Any]:
    """
    Set the user's main question or goal for this review.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.
        concern (string, required): One short sentence with the user's primary question
            (e.g. "Is this serum safe during pregnancy?", "Rash on my cheek for two weeks").

    Returns:
        Updated intake status JSON with status saved.

    Use for the main ask only; use append_intake_note for extra facts. Then get_intake_status.
    """
    logger.info("TOOL CALL: set_primary_concern: request_id=%s", request_id)

    concern_text = str(concern or "").strip()
    if not concern_text:
        return {
            "status": "error",
            "detail": "concern must be a non-empty string",
            "request_id": request_id,
        }

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    if session.primary_concern.strip() == concern_text:
        updated, persist_error = _persist_intake_session(session, profile)
        if persist_error:
            return persist_error
        assert updated is not None
        result = _format_intake_status(updated, profile, request_id=request_id)
        result["status"] = "unchanged"
        result["instruction"] = (
            "primary_concern is already set. Follow next_recommended_tool. "
            "Do NOT call set_primary_concern again this turn."
        )
        return result

    session = session.model_copy(update={"primary_concern": concern_text})
    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "saved"
    return result


@tool
def append_intake_note(
    request_id: OrchestratorRequestIdArg,
    note: IntakeNoteArg,
) -> dict[str, Any]:
    """
    Append one consultation detail gathered from the user.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.
        note (string, required): One short fact (symptom, duration, recent product change,
            e.g. "itching for 2 weeks", "started retinol 10 days ago").

    Returns:
        Updated intake status JSON with status saved.

    Do not repeat the primary concern (use set_primary_concern for that). Then get_intake_status.
    """
    logger.info("TOOL CALL: append_intake_note: request_id=%s", request_id)

    note_text = str(note or "").strip()
    if not note_text:
        return {
            "status": "error",
            "detail": "note must be a non-empty string",
            "request_id": request_id,
        }

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    notes = list(session.consultation_notes)
    notes.append(note_text)
    session = session.model_copy(update={"consultation_notes": notes})

    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "saved"
    return result


@tool
def confirm_reproductive_status(request_id: OrchestratorRequestIdArg) -> dict[str, Any]:
    """
    Confirm pregnancy and breastfeeding status was discussed and recorded.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.

    Returns:
        Updated intake status JSON with status saved.

    Call only after asking the user and setting pregnancy/breastfeeding via update_skin_profile
    when applicable. Required for skin_review, product_review, and combined before routing.
    """
    logger.info("TOOL CALL: confirm_reproductive_status: request_id=%s", request_id)

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    if session.reproductive_status_confirmed:
        updated, persist_error = _persist_intake_session(session, profile)
        if persist_error:
            return persist_error
        assert updated is not None
        result = _format_intake_status(updated, profile, request_id=request_id)
        result["status"] = "unchanged"
        result["instruction"] = (
            "Reproductive status already confirmed. Follow next_recommended_tool. "
            "Do NOT call confirm_reproductive_status again this turn."
        )
        return result

    session = session.model_copy(update={"reproductive_status_confirmed": True})
    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "saved"
    return result


@tool
def enque_request_for_auditing_stream(
    request_id: OrchestratorRequestIdArg,
    audit_type: AuditTypeArg,
    collected_context: AuditCollectedContextArg = "",
) -> dict[str, Any]:
    """
    Send a registered intake image to the specialist audit team (MedGemma).

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator turn
            (used to resolve client and intake session; not the image message id).
        audit_type (string, required): skin or product — which registered intake slot to enqueue.
        collected_context (string, optional): Extra short context; server merges intake summary.
            Default empty.

    Returns:
        JSON with status (queued, already_queued, blocked_not_ready, blocked_wrong_audit_type,
        blocked_missing_image, error), audit_type, image_request_id, instruction.

    Call only when get_intake_status shows ready_to_route true. For combined intent, call twice
    (audit_type skin then product). Do not call repeatedly on success for the same audit_type.
    """

    logger.info(
        "TOOL CALL: enque_request_for_auditing_stream: request_id=%s, audit_type=%s, "
        "collected_context=%s",
        request_id,
        audit_type,
        collected_context,
    )

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {
            "status": "error",
            "detail": f"failed to load intake session: {exc}",
            "request_id": request_id,
        }

    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None
    session = updated

    if session.phase == IntakePhase.AWAITING_AUDIT:
        return {
            "status": "already_queued",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "instruction": (
                "All required image audits were already dispatched. Do NOT call this tool again. "
                "Tell the user their photo(s) are being reviewed."
            ),
        }

    if not is_ready_to_route(session, profile):
        return {
            "status": "blocked_not_ready",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "missing_for_routing": list(session.missing_for_routing),
            "instruction": (
                "Intake is not ready to route. Call get_intake_status, complete missing items, "
                "then enqueue."
            ),
        }

    if not _audit_type_allowed(session, audit_type):
        expected = ", ".join(cat.value for cat in _required_audit_categories(session))
        return {
            "status": "blocked_wrong_audit_type",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "service_intent": session.service_intent.value if session.service_intent else None,
            "instruction": (
                f"audit_type={audit_type.value} does not match service intent. "
                f"Expected audit types: {expected or 'none (general intent cannot enqueue audits)'}."
            ),
        }

    image_ref = session.images.get(audit_type.value)
    if image_ref is None:
        return {
            "status": "blocked_missing_image",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "instruction": (
                f"No {audit_type.value} image registered. Call register_intake_image for that "
                "category before enqueueing."
            ),
        }

    image_envelope = _get_cached_ingress_envelope(image_ref.request_id)
    if not image_envelope:
        return {
            "status": "error",
            "detail": "ingress cache not found for registered image request_id",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
        }

    image_url = str(image_ref.image_url or "").strip()
    if not image_url:
        try:
            payload_obj: dict[str, Any] = json.loads(str(image_envelope.get("payload") or "{}"))
        except Exception:
            payload_obj = {}
        image_url = _resolve_image_url_from_payload(payload_obj)

    if not image_url:
        logger.info(
            "audit tool blocked (image url unresolved): image_request_id=%s audit_type=%s",
            image_ref.request_id,
            audit_type.value,
        )
        return {
            "status": "blocked_missing_image",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
            "instruction": (
                "Registered image URL could not be resolved. Ask the user to upload the image "
                "again and call register_intake_image before enqueueing."
            ),
        }

    context_text = _build_audit_context_text(session, profile, audit_type, collected_context)
    group_id, group_expected_count = _enqueue_group_params(session)

    dedupe_key = _audit_enqueued_dedupe_key(image_ref.request_id)
    if _redis is not None and not _redis.set(dedupe_key, "1", nx=True, ex=_AUDIT_ENQUEUED_TTL_SEC):
        logger.info(
            "audit request already dispatched: image_request_id=%s audit_type=%s",
            image_ref.request_id,
            audit_type.value,
        )
        instruction = _enqueue_audit_instruction(session, audit_type, "already_queued")
        if _all_required_audits_enqueued(session):
            _mark_intake_awaiting_audit(session, profile)
        return {
            "status": "already_queued",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
            "instruction": instruction or (
                "This image audit request was already dispatched. Do NOT call this tool again."
            ),
        }

    try:
        result = _dispatch_audit_image_ref(
            request_id=image_ref.request_id,
            case_id=str(image_envelope.get("case_id") or case_id),
            client_id=str(image_envelope.get("client_id") or client_id),
            message_id=str(image_envelope.get("message_id") or image_ref.request_id),
            channel=str(image_envelope.get("channel") or ""),
            image_url=image_url,
            context=context_text,
            group_id=group_id,
            group_expected_count=group_expected_count,
        )
    except httpx.HTTPStatusError as exc:
        if _redis is not None:
            _redis.delete(dedupe_key)
        status_code = exc.response.status_code if exc.response is not None else 0
        return {
            "status": "error",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
            "detail": f"audit dispatch failed with HTTP {status_code}",
        }
    except httpx.HTTPError as exc:
        if _redis is not None:
            _redis.delete(dedupe_key)
        return {
            "status": "error",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
            "detail": f"audit dispatch failed: {exc}",
        }

    if result.get("status") == "under_pressure":
        if _redis is not None:
            try:
                _redis.delete(dedupe_key)
            except Exception:
                logger.exception(
                    "failed to clear dedupe key after backpressure: image_request_id=%s",
                    image_ref.request_id,
                )
        result["request_id"] = request_id
        result["image_request_id"] = image_ref.request_id
        result["audit_type"] = audit_type.value
        return result

    _emit_orchestrator_event(
        {
            "request_id": image_ref.request_id,
            "case_id": str(image_envelope.get("case_id") or case_id),
            "client_id": str(image_envelope.get("client_id") or client_id),
            "message_id": str(image_envelope.get("message_id") or image_ref.request_id),
            "channel": str(image_envelope.get("channel") or ""),
            "agent": "orchestrator",
            "status": "routed_for_image_auditing",
            "target_endpoint": "/audit_image_ref",
            "medgemma_job_id": result.get("medgemma_job_id", ""),
            "enqueue_source": "orchestrator_tool",
            "audit_type": audit_type.value,
            "group_id": group_id,
        }
    )

    if _all_required_audits_enqueued(session):
        _mark_intake_awaiting_audit(session, profile)

    result["request_id"] = request_id
    result["image_request_id"] = image_ref.request_id
    result["audit_type"] = audit_type.value
    instruction = _enqueue_audit_instruction(session, audit_type, str(result.get("status") or ""))
    if instruction:
        result["instruction"] = instruction
    return result


def _event_ts_ms(event_id: str) -> int:
    """Extract the millisecond timestamp from a Redis stream id of form `ms-seq`."""
    try:
        return int(str(event_id).split("-", 1)[0])
    except Exception:
        return 0


def _event_matches_case(event: dict, case_id: str, request_id: str) -> bool:
    """True when an event belongs to the requested case, by case_id or request_id."""
    if case_id and str(event.get("case_id") or "") == case_id:
        return True
    if request_id and str(event.get("request_id") or "") == request_id:
        return True
    return False


@tool
def read_status_of_a_case(case_id: str, request_id: str = "") -> dict:
    """
    Read the current real-time status of a case from the events stream.

    Scans the events stream from newest to oldest in bounded pages and returns
    the latest event for the case plus a short recent history. Matching is by
    `case_id`, falling back to `request_id` when provided, because some events
    (e.g. downstream errors) may only carry one of the two.

    Args:
        case_id: Case identifier to read status for. Required.
        request_id: Optional request identifier used as a fallback matcher.

    Returns:
        A dict with:
            - `status`: `found`, `not_found`, or `error`
            - `case_id`, `request_id`
            - `latest`: dict of the most recent matching event (agent, event_status,
              message_id, channel, medgemma_status, medgemma_job_id, error,
              event_id, updated_at_ms) when found
            - `history`: list of up to CASE_STATUS_HISTORY recent matching events,
              newest first
            - `scanned`: number of events scanned
    """
    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured"}

    if not case_id and not request_id:
        return {"status": "error", "detail": "case_id or request_id is required"}

    matches: list[dict] = []
    scanned = 0
    cursor = "+"
    logger.info(f"TOOL CALL: read_status_of_a_case: case_id={case_id}, request_id={request_id}")

    try:
        while scanned < CASE_STATUS_SCAN_MAX:
            page = _redis.xrevrange(
                EVENTS_STREAM,
                max=cursor,
                min="-",
                count=CASE_STATUS_SCAN_PAGE,
            )
            if not page:
                break

            for event_id, event_data in page:
                scanned += 1
                if _event_matches_case(event_data, case_id, request_id):
                    matches.append({
                        "event_id": str(event_id),
                        "updated_at_ms": _event_ts_ms(event_id),
                        "agent": event_data.get("agent", "unknown"),
                        "event_status": event_data.get("status", "unknown"),
                        "case_id": event_data.get("case_id", ""),
                        "request_id": event_data.get("request_id", ""),
                        "message_id": event_data.get("message_id", ""),
                        "channel": event_data.get("channel", ""),
                        "audit_status": event_data.get("medgemma_status", ""),
                        "audit_job_id": event_data.get("medgemma_job_id", ""),
                        # Backward compatibility keys
                        "diagnose_status": event_data.get("medgemma_status", ""),
                        "diagnose_job_id": event_data.get("medgemma_job_id", ""),
                        "error": event_data.get("error", ""),
                    })
                    if len(matches) >= CASE_STATUS_HISTORY:
                        break

            if len(matches) >= CASE_STATUS_HISTORY:
                break

            # Advance cursor to just before the oldest id on this page.
            last_id = str(page[-1][0])
            ms, _, seq = last_id.partition("-")
            try:
                seq_int = int(seq or "0")
            except ValueError:
                seq_int = 0
            if seq_int > 0:
                cursor = f"{ms}-{seq_int - 1}"
            else:
                ms_int = int(ms or "0")
                if ms_int <= 0:
                    break
                cursor = f"{ms_int - 1}-18446744073709551615"
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"events stream scan failed: {exc}",
            "case_id": case_id,
            "request_id": request_id,
            "scanned": scanned,
        }

    if not matches:
        return {
            "status": "not_found",
            "case_id": case_id,
            "request_id": request_id,
            "scanned": scanned,
            "instruction": (
                "No status events exist for this case yet. Do NOT call this tool again in this turn. "
                "Tell the user their request is still being processed and results will follow shortly."
            ),
        }

    return {
        "status": "found",
        "case_id": case_id,
        "request_id": request_id,
        "latest": matches[0],
        "history": matches,
        "scanned": scanned,
        "instruction": (
            "This is the current case status. Do NOT call this tool again in this turn. "
            "Use the latest status to compose your reply to the user."
        ),
    }



@tool
def read_soul_md() -> str:
    """
    Read the soul.md file.
    Use this tool when you need to understand your inner self, the role you play beyond the immediate
    role of dermatological diagnosis. Its helpful when you need to engage with your innermost values as part of the large goal
    of serving humanity.
    
    The Soul.md file has contents such as:
        - Core Identity
        - Diagnostic Authority
        - Your Personality
        - Your Origin
    Returns:
        The content of the soul.md file
    """
    global _SOUL_MD_CACHE
    logger.info(f"TOOL CALL: read_soul_md")

    if _SOUL_MD_CACHE is None:
        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "agents", "soul.md")
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                _SOUL_MD_CACHE = f.read()
        except OSError as exc:
            logger.exception("failed to read soul.md: path=%s", path)
            return (
                f"Soul context is temporarily unavailable ({exc}). "
                "Do NOT call this tool again in this turn. Proceed to respond to the user "
                "using your established identity and values."
            )

    # Append a terminal directive so the agent loads identity context once and
    # proceeds, rather than re-calling this static tool and exhausting the
    # recursion budget.
    return (
        f"{_SOUL_MD_CACHE}\n\n"
        "[SYSTEM NOTE: Your soul/identity context is now loaded. Do NOT call read_soul_md "
        "again in this turn. Proceed to respond to the user.]"
    )



@tool
def skin_profile_is_available(client_id: OrchestratorClientIdArg) -> bool:
    """
    Check whether a skin profile already exists for this client.

    Arguments:
        client_id (string, required): Exact client_id from the orchestrator input.

    Returns:
        true if a profile exists in cache or Firestore, false otherwise.
    """
    cid = _normalize_client_id(client_id)
    if not cid:
        return False

    cache_key = _skin_profile_cache_key(cid)
    if _redis is not None:
        try:
            cached_profile = run_with_redis_retries(
                lambda: _redis.get(cache_key),
                operation_name="skin_profile_get",
            )
            if _deserialize_skin_profile(cached_profile):
                return True
        except Exception:
            logger.exception("failed to read skin profile cache: client_id=%s", cid)

    firestore_client = _get_firestore_client()
    if firestore_client is None:
        return False

    try:
        client_skin_profile = firestore_client.collection("skin_profiles").document(cid).get()
    except Exception:
        logger.exception("failed to read skin profile from firestore: client_id=%s", cid)
        return False

    if not client_skin_profile.exists:
        return False

    profile_dict = client_skin_profile.to_dict() or {}
    if _redis is not None and isinstance(profile_dict, dict) and profile_dict:
        try:
            run_with_redis_retries(
                lambda: _redis.setex(
                    cache_key,
                    _SKIN_PROFILE_CACHE_TTL_SEC,
                    _serialize_skin_profile(profile_dict),
                ),
                operation_name="skin_profile_set",
            )
        except Exception:
            logger.exception("failed to cache skin profile from firestore: client_id=%s", cid)
    return True


@tool(args_schema=UpdateSkinProfileInput)
def update_skin_profile(
    client_id: str,
    age: Optional[int] = None,
    gender: Optional[str] = None,
    location: Optional[str] = None,
    skin_type: Optional[str] = None,
    current_products: Optional[list[str]] = None,
    current_medications: Optional[list[str]] = None,
    current_conditions: Optional[list[str]] = None,
    current_goals: Optional[list[str]] = None,
    allergies: Optional[list[str]] = None,
    pregnancy: Optional[bool] = None,
    breastfeeding: Optional[bool] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Create or update the client's long-lived skin profile (merge partial updates).

    Arguments:
        client_id (string, required): Exact client_id from the orchestrator input.
        age, gender, location, skin_type, current_products, current_medications,
        current_conditions, current_goals, allergies, pregnancy, breastfeeding, notes:
            Optional profile fields; include only what the user provided.

    Returns:
        Full profile dict on success, or {status: error, detail: ...} on failure.

    Call get_skin_profile first if a profile may exist. Pass at least one profile field.
    """
    skin_profile = SkinProfileUpdate(
        age=age,
        gender=gender,
        location=location,
        skin_type=skin_type,
        current_products=current_products,
        current_medications=current_medications,
        current_conditions=current_conditions,
        current_goals=current_goals,
        allergies=allergies,
        pregnancy=pregnancy,
        breastfeeding=breastfeeding,
        notes=notes,
    )
    cid = _normalize_client_id(client_id)
    if not cid:
        return {"status": "error", "detail": "client_id is required"}

    if not skin_profile.model_dump(exclude_none=True):
        return {"status": "error", "detail": "skin_profile must include at least one field to update"}

    try:
        existing = _fetch_skin_profile_dict(cid)
        validated_profile = _merge_skin_profile(existing, skin_profile)
    except Exception as exc:
        return {"status": "error", "detail": f"invalid skin_profile payload: {exc}"}

    firestore_client = _get_firestore_client()
    if firestore_client is None:
        return {"status": "error", "detail": "firestore is unavailable"}

    try:
        firestore_client.collection("skin_profiles").document(cid).set(validated_profile)
    except Exception:
        logger.exception("failed to write skin profile to firestore: client_id=%s", cid)
        return {"status": "error", "detail": "failed to persist skin profile"}

    if _redis is not None:
        try:
            run_with_redis_retries(
                lambda: _redis.setex(
                    _skin_profile_cache_key(cid),
                    _SKIN_PROFILE_CACHE_TTL_SEC,
                    _serialize_skin_profile(validated_profile),
                ),
                operation_name="skin_profile_set",
            )
        except Exception:
            logger.exception("failed to cache updated skin profile: client_id=%s", cid)

    return validated_profile

@tool
def get_skin_profile(client_id: OrchestratorClientIdArg) -> Dict[str, Any]:
    """
    Load the client's stored skin profile.

    Arguments:
        client_id (string, required): Exact client_id from the orchestrator input.

    Returns:
        Profile dict with keys: age, gender, location, skin_type, current_products,
        current_medications, current_conditions, current_goals, allergies,
        pregnancy, breastfeeding, notes. Empty dict if none exists.
    """
    cid = _normalize_client_id(client_id)
    if not cid:
        return {}
    return _fetch_skin_profile_dict(cid)


def _get_tavily_client() -> TavilyClient | None:
    if not TAVILY_API_KEY:
        return None
    return TavilyClient(api_key=TAVILY_API_KEY)


def _format_tavily_results(result: dict[str, Any], *, max_results: int) -> str:
    """
    Convert Tavily results into compact JSON safe for an LLM context window.

    Args:
        result: Raw Tavily response.
        max_results: Maximum number of result entries to include.

    Returns:
        Compact JSON containing only title, URL, and a bounded content snippet.
    """
    raw_results = result.get("results") or []
    summaries: list[dict[str, str]] = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("raw_content") or "").strip()
        summaries.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "content": content[:SEARCH_RESULT_CONTENT_CHARS],
            }
        )

    payload = {
        "query": str(result.get("query") or ""),
        "results": summaries,
    }
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(text) <= SEARCH_RESULT_TOTAL_CHARS:
        return text
    return text[:SEARCH_RESULT_TOTAL_CHARS] + "...<truncated>"


def _search_cache_key(
    *, label: str, query: str, include_domains: list[str], max_results: int, search_depth: str
) -> str:
    """Build a deterministic Redis cache key for a search invocation."""
    raw = json.dumps(
        {
            "l": label,
            "q": query.strip().lower(),
            "d": sorted(include_domains),
            "n": max_results,
            "s": search_depth,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{_SEARCH_CACHE_PREFIX}:{digest}"


def _run_tavily_search(
    *,
    label: str,
    query: str,
    max_results: int,
    include_domains: list[str],
    search_depth: str = "",
) -> str:
    """
    Execute a Tavily search with caching, a bounded timeout, and a terminal
    failure directive so the agent degrades gracefully instead of looping.

    Args:
        label: Human-readable tool label used in logs and cache keys.
        query: Search query string.
        max_results: Already-capped number of results to retrieve.
        include_domains: Domain allowlist for the search.
        search_depth: Optional Tavily search depth (e.g. "advanced").

    Returns:
        Compact JSON results string, a not-configured notice, or a terminal
        failure directive string.
    """
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    logger.info("TOOL CALL: %s search: query=%s, max_results=%s", label, query, max_results)

    cache_key = _search_cache_key(
        label=label,
        query=query,
        include_domains=include_domains,
        max_results=max_results,
        search_depth=search_depth,
    )
    if _redis is not None and SEARCH_CACHE_TTL_S > 0:
        try:
            cached = _redis.get(cache_key)
            if cached:
                logger.info("TOOL CALL CACHE HIT: %s search: query=%s", label, query)
                return str(cached)
        except Exception:
            logger.exception("failed to read search cache: key=%s", cache_key)

    search_kwargs: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "include_domains": include_domains,
        "timeout": TAVILY_TIMEOUT_S,
    }
    if search_depth:
        search_kwargs["search_depth"] = search_depth

    try:
        result = client.search(**search_kwargs)
        formatted = _format_tavily_results(result, max_results=max_results)
    except Exception as exc:
        logger.warning("%s search failed: query=%s err=%s", label, query, exc)
        return (
            f"{label} search failed ({exc}). Do NOT call this tool again in this turn. "
            "Answer the user from your existing knowledge and clearly note that live "
            "research was unavailable."
        )

    if _redis is not None and SEARCH_CACHE_TTL_S > 0 and formatted:
        try:
            _redis.setex(cache_key, SEARCH_CACHE_TTL_S, formatted)
        except Exception:
            logger.exception("failed to write search cache: key=%s", cache_key)

    logger.info(
        "TOOL CALL SUCCESS: %s search: query=%s result_chars=%s",
        label,
        query,
        len(formatted),
    )
    return formatted


@tool
def wikipedia_search(query: str, max_results: int = 3) -> str:
    """
    Search Wikipedia content through Tavily.

    Use this tool when you need background explanations, definitions, and
    high-level context. Do not rely on this tool alone for critical medical
    safety claims; cross-check key claims with broader web sources.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
    """
    return _run_tavily_search(
        label="Wikipedia",
        query=query,
        max_results=max(1, min(max_results, 5)),
        include_domains=["wikipedia.org"],
    )


@tool
def internet_search(query: str, max_results: int = 3) -> str:
    """
    Perform general web research through Tavily.

    Use this as the primary research tool for up-to-date and credible external
    sources (guidelines, journals, hospital/public-health pages, and trusted
    references). Prefer this tool for safety checks and practical guidance.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
    """
    return _run_tavily_search(
        label="Internet",
        query=query,
        max_results=max(1, min(max_results, 3)),
        include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "mydawa.com"],
        search_depth="advanced",
    )


@tool
def check_medgemma_health() -> dict[str, str]:
    """
    Check whether the image-audit (MedGemma) service is reachable before routing.

    Use this BEFORE enqueueing an image audit when you are unsure the analysis
    service is available, or after a prior dispatch reported pressure. It performs
    a lightweight, non-mutating reachability probe and never enqueues anything.

    Returns:
        A dict with:
            - `status`: `healthy`, `degraded`, or `unavailable`
            - `endpoint`: the probed gateway URL
            - `retry_after_seconds`: present when the service is rate-limited
            - `instruction`: terminal guidance on whether to enqueue now
    """
    logger.info("TOOL CALL: check_medgemma_health")

    base = MEDGEMMA_GATEWAY_URL.rstrip("/")
    # `/jobs/{id}` returns 404 for unknown ids; any HTTP response proves the
    # gateway is reachable without creating or mutating a job.
    endpoint = f"{base}/jobs/__health_probe__"
    timeout = httpx.Timeout(
        timeout=MEDGEMMA_HEALTH_TIMEOUT_S,
        connect=MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S,
    )

    try:
        response = httpx.get(endpoint, timeout=timeout)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        return {
            "status": "unavailable",
            "endpoint": endpoint,
            "detail": f"health probe failed: {exc}",
            "instruction": (
                "The image-audit service is currently unreachable. Do NOT enqueue an image audit now. "
                "Tell the user the analysis service is temporarily unavailable and to try again shortly."
            ),
        }

    if response.status_code == 429:
        retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After", ""))
        return {
            "status": "degraded",
            "endpoint": endpoint,
            "retry_after_seconds": retry_after or "1",
            "instruction": (
                "The image-audit service is under heavy load. Avoid enqueueing right now and "
                "tell the user that processing may be delayed."
            ),
        }

    if response.status_code >= 500:
        return {
            "status": "degraded",
            "endpoint": endpoint,
            "detail": f"health probe returned HTTP {response.status_code}",
            "instruction": (
                "The image-audit service reports a degraded state. Enqueue only if necessary and "
                "warn the user of possible delays."
            ),
        }

    # 2xx and 4xx (including the expected 404) both prove the gateway is up.
    return {
        "status": "healthy",
        "endpoint": endpoint,
        "instruction": (
            "The image-audit service is reachable. You may enqueue image audits as needed. "
            "Do NOT call this tool again in this turn."
        ),
    }



orchestrator_tools: list = [
    get_time,
    skin_profile_is_available,
    get_skin_profile,
    update_skin_profile,
    get_intake_status,
    set_service_intent,
    register_intake_image,
    set_primary_concern,
    append_intake_note,
    confirm_reproductive_status,
    enque_request_for_auditing_stream,
    read_status_of_a_case,
    check_medgemma_health,
    read_soul_md,
    wikipedia_search,
    internet_search,
]



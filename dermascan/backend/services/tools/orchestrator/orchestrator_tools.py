"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


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
from typing import Any, Dict, Optional, Annotated

import httpx
from langchain.tools import tool
from redis import Redis
from tavily import TavilyClient
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries
from dermascan.shared.escalation_image import normalize_image_blob_path
from dermascan.shared.firebase_storage import sign_client_image_blob_path
from dermascan.shared.ingress_payload import (
    payload_has_image_attachment,
    resolve_first_image_blob_path_from_payload,
    resolve_first_image_url_from_payload,
    resolve_image_urls_from_payload,
)
from dermascan.ai_service.postprocessing.escalation import (
    ESCALATION_CLIENT_MESSAGE,
    dispatch_consent_escalation,
    dispatch_user_requested_escalation,
    has_pending_consent_escalation_offer,
)
from dermascan.shared.escalation_anchor import (
    dermatologist_escalation_readiness,
    record_image_anchor,
    record_ingress_media_anchor,
)
from dermascan.shared.cosmetic_feature_flags import (
    cosmetic_consent_escalation_active,
    cosmetic_pivot_enabled,
    cosmetic_skin_first_routing_active,
    cosmetic_text_product_review_enabled,
)
from dermascan.shared.cosmetic_types import PersonalizationMode, normalize_personalization_mode
from dermascan.shared.intake_session import (
    IntakeImageCategory,
    IntakeImageRef,
    IntakePhase,
    IntakeSession,
    LIST_PROFILE_FIELDS,
    MISSING_PRIMARY_CONCERN,
    MISSING_PRODUCT_IMAGE,
    MISSING_PROFILE_MINIMUM,
    MISSING_MEDICATIONS_CONTEXT,
    MISSING_REPRODUCTIVE_STATUS,
    MISSING_SERVICE_INTENT,
    MISSING_SKIN_IMAGE,
    MISSING_SKIN_SNAPSHOT,
    ServiceIntent,
    build_collected_context_summary,
    has_product_image,
    recompute_session,
)
from dermascan.shared.intake_session_store import (
    IntakeSessionStoreError,
    archive_intake_session,
    delete_intake_session,
    get_or_create_intake_session,
    load_archived_intake_session,
    load_intake_session,
    recompute_intake_session,
    save_intake_session_recomputed,
)
from dermascan.shared.orchestrator_checkpoint_reset import reset_orchestrator_thread_checkpoint
from dermascan.shared.turn_contact_api import return_to_mydawa_main_menu_sync
from dermascan.backend.services.agents.text_product_review import (
    dispatch_text_product_review_signal,
    is_text_product_review_enqueued,
    text_product_review_eligible,
)
from dermascan.shared.firebase_storage import initialize_firebase_app
from dermascan.shared.load_env import ensure_env_loaded

ensure_env_loaded()

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
MEDGEMMA_AUDIT_CONTEXT_MAX_CHARS: int = max(
    200,
    int(os.getenv("MEDGEMMA_AUDIT_CONTEXT_MAX_CHARS", "800")),
)


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
    Partial skin profile update payload merged by the update_intake tool.

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


# Skin-profile field names shared by the consolidated update_intake tool. Any of
# these, when provided, is merged into the client's long-lived skin profile.
_SKIN_PROFILE_FIELD_NAMES: tuple[str, ...] = (
    "age",
    "gender",
    "location",
    "skin_type",
    "current_products",
    "current_medications",
    "current_conditions",
    "current_goals",
    "allergies",
    "pregnancy",
    "breastfeeding",
    "notes",
)


class UpdateIntakeInput(BaseModel):
    """
    Flat tool input for the consolidated update_intake tool.

    One call records any subset of intake facts in a single load-mutate-save cycle:
    service intent, primary concern, consultation notes, and long-lived skin-profile
    fields. Providing pregnancy or breastfeeding (or confirm_reproductive_status)
    implicitly confirms reproductive status. All object parameters are flattened to
    top-level optional fields because OpenAI strict tool schemas reject nested objects.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description=(
            "Required. Exact request_id from the current orchestrator input "
            "(copy the value shown after 'request_id:'). "
            "Do not use client_id, case_id, or message_id."
        ),
    )
    service_intent: Optional[ServiceIntent] = Field(
        default=None,
        description=(
            "Optional. User's chosen service when they pick a menu option or state a goal: "
            "skin_review, product_review, combined, or general."
        ),
    )
    primary_concern: Optional[str] = Field(
        default=None,
        description=(
            "Optional. User's main question or goal in one short sentence "
            "(e.g. 'Is this serum safe during pregnancy?')."
        ),
    )
    consultation_notes: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional. Short consultation facts to append (symptom, timeline, routine change), "
            "e.g. ['itching for 2 weeks', 'started retinol 10 days ago']. "
            "Do not put the main question here (use primary_concern) or profile data "
            "like goals/allergies (use the profile fields)."
        ),
    )
    age: Optional[int] = Field(default=None, ge=0, description="Profile: client age in years.")
    gender: Optional[str] = Field(
        default=None,
        description="Profile: gender or sex relevant to care (e.g. female, male, non-binary).",
    )
    location: Optional[str] = Field(
        default=None,
        description="Profile: city, region, or climate context when relevant to skin care.",
    )
    skin_type: Optional[str] = Field(
        default=None,
        description="Profile: self-reported skin type (oily, dry, combination, sensitive, normal).",
    )
    current_products: Optional[list[str]] = Field(
        default=None,
        description=(
            "Profile: skincare or cosmetic products currently in the client's routine. "
            "Pass an empty list [] when the user says they use no products — that records "
            "the answer and clears the intake gap."
        ),
    )
    current_medications: Optional[list[str]] = Field(
        default=None,
        description="Profile: current medications that may affect skin (topical or systemic).",
    )
    current_conditions: Optional[list[str]] = Field(
        default=None,
        description="Profile: active or chronic skin or related health conditions.",
    )
    current_goals: Optional[list[str]] = Field(
        default=None,
        description="Profile: client skincare goals or concerns (e.g. acne, hyperpigmentation).",
    )
    allergies: Optional[list[str]] = Field(
        default=None,
        description=(
            "Profile: known allergies or ingredient sensitivities. "
            "Pass an empty list [] when the user says they have none — that records "
            "the answer and clears the intake gap."
        ),
    )
    pregnancy: Optional[bool] = Field(
        default=None,
        description=(
            "Profile: whether the client is currently pregnant. "
            "Providing this (true or false) confirms reproductive status."
        ),
    )
    breastfeeding: Optional[bool] = Field(
        default=None,
        description=(
            "Profile: whether the client is currently breastfeeding. "
            "Providing this (true or false) confirms reproductive status."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description="Profile: free-text intake notes (onset, symptoms, spread, recent changes).",
    )
    confirm_reproductive_status: Optional[bool] = Field(
        default=None,
        description=(
            "Optional. Set true to confirm pregnancy/breastfeeding was discussed when you are "
            "not changing those fields. Implied automatically when pregnancy or breastfeeding "
            "is provided."
        ),
    )
    skin_snapshot_skipped: Optional[bool] = Field(
        default=None,
        description=(
            "Optional. Set true when the user explicitly declines a skin photo and accepts "
            "a general (non-tailored) product safety check. Sets personalization to generic."
        ),
    )
    personalization_mode: Optional[PersonalizationMode] = Field(
        default=None,
        description=(
            "Optional. Override personalization mode: personalized, profile_only, or generic. "
            "Normally derived from skin_snapshot_skipped; only set when explicitly needed."
        ),
    )
    product_ref_text: Optional[str] = Field(
        default=None,
        description=(
            "Optional. Product name or text reference when the user provides a product "
            "without a label photo (cosmetic pivot routing). Does not replace label photo "
            "for specialist enqueue until text-only review is enabled."
        ),
    )

    def to_profile_update(self) -> SkinProfileUpdate:
        """
        Build a partial SkinProfileUpdate from the profile fields provided.

        Returns:
            SkinProfileUpdate: Only the skin-profile fields explicitly set on this input.
        """
        provided = self.model_dump(include=set(_SKIN_PROFILE_FIELD_NAMES), exclude_none=True)
        return SkinProfileUpdate(**provided)

    def has_profile_update(self) -> bool:
        """
        Return True when any skin-profile field was provided.

        Returns:
            bool: True when at least one profile field is set.
        """
        return bool(self.model_dump(include=set(_SKIN_PROFILE_FIELD_NAMES), exclude_none=True))

    def implies_reproductive_confirmation(self) -> bool:
        """
        Return True when this update should confirm reproductive status.

        Returns:
            bool: True when confirm_reproductive_status is set or pregnancy/breastfeeding provided.
        """
        return (
            self.confirm_reproductive_status is True
            or self.pregnancy is not None
            or self.breastfeeding is not None
        )

    def confirmed_none_profile_fields(self) -> list[str]:
        """
        List list-valued profile fields explicitly provided as empty this call.

        An explicit ``[]`` means the user confirmed they have none, which must
        clear the corresponding intake gap.

        Returns:
            list[str]: Field names confirmed as "none".
        """
        return [
            name
            for name in LIST_PROFILE_FIELDS
            if getattr(self, name) is not None and not getattr(self, name)
        ]


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

from dermascan.billing.config import BillingJobType
from dermascan.billing.meter import job_credits_shortfall, record_job_charge
from dermascan.backend.services.tools.orchestrator.billing_tools import (
    create_topup,
    get_credit_balance,
)

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
            "(audit_type=skin then audit_type=product) on the current-turn request_id."
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


def _persist_skin_profile(
    cid: str,
    updates: SkinProfileUpdate,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Merge partial profile updates onto the stored profile and persist them.

    Writes the validated profile to Firestore and refreshes the Redis cache.

    Args:
        cid: Normalized client id.
        updates: Partial profile update to merge and persist.

    Returns:
        tuple[dict | None, dict | None]: ``(validated_profile, None)`` on success,
        or ``(None, error_dict)`` when validation or persistence fails.
    """
    try:
        existing = _fetch_skin_profile_dict(cid)
        validated_profile = _merge_skin_profile(existing, updates)
    except Exception as exc:
        return None, {"status": "error", "detail": f"invalid skin_profile payload: {exc}"}

    firestore_client = _get_firestore_client()
    if firestore_client is None:
        return None, {"status": "error", "detail": "firestore is unavailable"}

    try:
        firestore_client.collection("skin_profiles").document(cid).set(validated_profile)
    except Exception:
        logger.exception("failed to write skin profile to firestore: client_id=%s", cid)
        return None, {"status": "error", "detail": "failed to persist skin profile"}

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

    return validated_profile, None


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
        try:
            payload_obj = json.loads(str(payload or "{}"))
        except Exception:
            payload_obj = {}
        if payload_has_image_attachment(payload_obj):
            blob_path = resolve_first_image_blob_path_from_payload(payload_obj, redis_client=_redis)
            record_ingress_media_anchor(
                _redis,
                client_id=str(client_id or "anonymous"),
                anchor_request_id=rid,
                case_id=str(case_id or rid),
                message_id=str(message_id or rid),
                channel=str(channel or "whatsapp"),
                image_blob_path=blob_path,
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


def _intake_stop_suffix(session: IntakeSession | None = None) -> str:
    """
    Standard instruction suffix to prevent tool-call loops within one turn.

    When intake is ready to route but not yet enqueued, require enqueue before reply.

    Args:
        session: Current intake session, if known.

    Returns:
        str: Anti-loop or enqueue-first guidance for tool responses.
    """
    if (
        session is not None
        and not session.missing_for_routing
        and session.phase not in (IntakePhase.AWAITING_AUDIT, IntakePhase.DELIVERED)
    ):
        return (
            " Do NOT send your final reply yet. "
            "You MUST call enque_request_for_auditing_stream first (see instruction above). "
            "Only after enqueue returns queued or already_queued may you tell the user "
            "their review is in progress and compose your final reply."
        )
    return (
        " Then compose your final reply to the user in this turn. "
        "The latest intake status is included in this tool result and in the turn context; "
        "do NOT re-read it. "
        "Do NOT repeat a tool that already returned saved/registered/queued/unchanged."
    )


def _skin_prerequisite_enqueue_eligible(session: IntakeSession) -> bool:
    """
    Return True when a skin audit may be enqueued as a product-review prerequisite.

    Args:
        session: Current intake session.

    Returns:
        bool: True when skin-first routing requires a snapshot before product review.
    """
    if not cosmetic_skin_first_routing_active():
        return False
    if session.service_intent != ServiceIntent.PRODUCT_REVIEW:
        return False
    if session.skin_snapshot_skipped:
        return False
    if MISSING_SKIN_SNAPSHOT not in session.missing_for_routing:
        return False
    if session.images.get(IntakeImageCategory.SKIN.value) is None:
        return False
    blocking = [item for item in session.missing_for_routing if item != MISSING_SKIN_SNAPSHOT]
    return not blocking


def _ready_for_audit_enqueue(session: IntakeSession, audit_type: IntakeImageCategory) -> bool:
    """
    Return True when the requested audit type may be enqueued for the current session.

    Args:
        session: Current intake session.
        audit_type: Audit category the agent is trying to enqueue.

    Returns:
        bool: True when enqueue should proceed.
    """
    if not session.missing_for_routing:
        return True
    if audit_type == IntakeImageCategory.SKIN and _skin_prerequisite_enqueue_eligible(session):
        return True
    return False


def _mark_skin_prerequisite_pending(
    session: IntakeSession,
    profile: dict[str, Any],
) -> IntakeSession:
    """
    Persist skin-prerequisite state and archive intake for signal delivery.

    Args:
        session: Intake session after skin prerequisite enqueue.
        profile: Skin profile mapping for recomputation.

    Returns:
        IntakeSession: Updated session when persistence succeeds; otherwise the input session.
    """
    pending_session = session.model_copy(
        update={
            "pending_product_after_skin": True,
            "phase": IntakePhase.AWAITING_AUDIT,
        }
    )
    updated, persist_error = _persist_intake_session(pending_session, profile)
    if persist_error or updated is None:
        return session
    _archive_intake_for_signal_delivery(updated)
    return updated


def _needs_product_label_photo_for_enqueue(session: IntakeSession) -> bool:
    """
    Return True when intake is ready but product enqueue still requires a label photo.

    Applies when cosmetic pivot accepts product name for routing but MedGemma label
    read (or Step 5 text-only path) is not yet available.

    Args:
        session: Current intake session.

    Returns:
        bool: True when the agent should ask for a product label photo before enqueue.
    """
    if not cosmetic_pivot_enabled():
        return False
    if cosmetic_text_product_review_enabled():
        return False
    if session.service_intent not in (ServiceIntent.PRODUCT_REVIEW, ServiceIntent.COMBINED):
        return False
    if has_product_image(session):
        return False
    return bool(session.product_ref_text.strip())


def _derive_intake_next_action(session: IntakeSession, profile: dict[str, Any]) -> str:
    """
    Recommend exactly one next tool for the orchestrator model.

    Args:
        session (IntakeSession): Current intake session.
        profile (dict[str, Any]): Skin profile mapping.

    Returns:
        str: Tool name the model should call next, or reply_only when done.
    """
    if not session.missing_for_routing:
        return "enque_request_for_auditing_stream"

    missing = session.missing_for_routing
    if MISSING_SKIN_SNAPSHOT in missing and _skin_prerequisite_enqueue_eligible(session):
        return "enque_request_for_auditing_stream"
    if (
        MISSING_SKIN_SNAPSHOT in missing
        or MISSING_SKIN_IMAGE in missing
        or MISSING_PRODUCT_IMAGE in missing
    ):
        return "register_intake_image"
    # Every remaining gap (service intent, primary concern, profile minimums,
    # reproductive status) is recorded through the consolidated update_intake tool.
    if (
        MISSING_SERVICE_INTENT in missing
        or MISSING_PRIMARY_CONCERN in missing
        or MISSING_PROFILE_MINIMUM in missing
        or MISSING_MEDICATIONS_CONTEXT in missing
        or MISSING_REPRODUCTIVE_STATUS in missing
    ):
        return "update_intake"
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
    stop = _intake_stop_suffix(session)
    next_tool = _derive_intake_next_action(session, profile)
    skip_suffix = ""
    if session.skin_snapshot_skipped:
        skip_suffix = (
            " User opted out of skin snapshot — generic product check only; "
            "do NOT ask for a skin photo."
        )

    if session.phase == IntakePhase.AWAITING_AUDIT:
        if session.pending_product_after_skin or _all_required_audits_enqueued(session):
            return (
                "Audits are already queued. Tell the user their photo is being reviewed. "
                "Do NOT call enque_request_for_auditing_stream again."
            )
        # Stale terminal phase (e.g. a new photo replaced an audited slot):
        # fall through to normal routing guidance so the audit gets dispatched.

    if not session.missing_for_routing:
        if _needs_product_label_photo_for_enqueue(session):
            return (
                "Product name is recorded. Ask the user for a clear, readable product label "
                "photo so the specialist team can run the review."
                f"{skip_suffix}{stop}"
            )
        if (
            text_product_review_eligible(session)
            and session.service_intent in (ServiceIntent.PRODUCT_REVIEW, ServiceIntent.COMBINED)
        ):
            audit_type = "product"
            return (
                f"Call enque_request_for_auditing_stream once with audit_type={audit_type} "
                f"to run the text-only product check (current turn request_id)."
                f"{skip_suffix}{stop}"
            )
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
            f"Call update_intake with the profile fields: {gaps}. "
            "Use values the user stated in their latest message. If the user said they "
            "have none for a list field (e.g. no products, no allergies), pass an empty "
            f"list [] for that field to record it.{stop}"
        )

    if MISSING_MEDICATIONS_CONTEXT in session.missing_for_routing:
        return (
            "Before skin snapshot review, ask whether the user is on any prescribed or "
            "over-the-counter skin/acne medications (topical or oral). Call update_intake "
            "with current_medications from their answer. If they have none, pass "
            "current_medications=[] to record that explicitly."
            f"{stop}"
        )

    if MISSING_REPRODUCTIVE_STATUS in session.missing_for_routing:
        return (
            "Call update_intake with pregnancy and breastfeeding from the user's answer "
            f"(providing them confirms reproductive status).{skip_suffix}{stop}"
        )

    if (
        MISSING_SKIN_SNAPSHOT in session.missing_for_routing
        and session.service_intent == ServiceIntent.PRODUCT_REVIEW
        and not session.skin_snapshot_skipped
    ):
        if _skin_prerequisite_enqueue_eligible(session):
            return (
                "Call enque_request_for_auditing_stream once with audit_type=skin "
                "(current turn request_id). Product check will run automatically after the "
                "skin snapshot completes. Tell the user both steps are queued."
                f"{stop}"
            )
        return (
            "Fresh skin snapshot required before product check. Ask for a close-up skin photo "
            "and call register_intake_image (skin) then enque_request_for_auditing_stream with "
            "audit_type=skin when ready. Product check will run automatically after skin review. "
            "OR offer generic product-only check: if user explicitly declines skin photo, call "
            "update_intake with skin_snapshot_skipped=true."
            f"{stop}"
        )

    if (
        MISSING_SKIN_IMAGE in session.missing_for_routing
        and session.service_intent == ServiceIntent.COMBINED
        and not session.skin_snapshot_skipped
    ):
        return (
            "Missing skin photo for combined check. Offer skin snapshot OR product-only "
            "generic safety check. If user explicitly declines skin photo, call update_intake "
            "with service_intent=product_review and skin_snapshot_skipped=true."
            f"{skip_suffix}{stop}"
        )

    if session.missing_for_routing:
        missing = ", ".join(session.missing_for_routing)
        return (
            f"Missing: {missing}. Call {next_tool} once using the latest user message."
            f"{skip_suffix}{stop}"
        )

    return f"Continue intake. Next tool: {next_tool}.{skip_suffix}{stop}"


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
    ready = not session.missing_for_routing

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
        "must_enqueue_before_reply": (
            ready and session.phase not in (IntakePhase.AWAITING_AUDIT, IntakePhase.DELIVERED)
        ),
        "collected_context_summary": session.collected_context_summary,
        "skin_snapshot_skipped": session.skin_snapshot_skipped,
        "personalization_mode": session.personalization_mode,
        "product_ref_text": session.product_ref_text,
        "next_recommended_tool": _derive_intake_next_action(session, profile),
        "instruction": _intake_instruction(session, profile),
    }


def compute_intake_guidance(client_id: str) -> dict[str, Any] | None:
    """
    Compute deterministic intake guidance for read-only prompt injection.

    Recomputes derived fields in memory (no persistence, no session creation) so the
    orchestrator prompt can carry the authoritative next step without the model calling
    a status tool. Returns None when no active session exists for the client.

    Args:
        client_id: Client identifier.

    Returns:
        dict[str, Any] | None: Guidance with phase, missing_for_routing, ready_to_route,
        next_recommended_tool, and instruction; None when there is no session yet.
    """
    cid = _normalize_client_id(client_id)
    if not cid:
        return None

    session = load_intake_session(cid, redis_client=_redis)
    if session is None:
        return None

    profile = _load_skin_profile_mapping(cid)
    recomputed = recompute_intake_session(session, profile)
    return {
        "phase": recomputed.phase.value,
        "missing_for_routing": list(recomputed.missing_for_routing),
        "ready_to_route": not recomputed.missing_for_routing,
        "skin_snapshot_skipped": recomputed.skin_snapshot_skipped,
        "personalization_mode": recomputed.personalization_mode,
        "product_ref_text": recomputed.product_ref_text,
        "next_recommended_tool": _derive_intake_next_action(recomputed, profile),
        "instruction": _intake_instruction(recomputed, profile),
    }


def _resolve_registration_image(
    envelope: dict[str, str],
    attachment_id: Optional[str] = None,
) -> tuple[str, str, str] | None:
    """
    Resolve one image blob path from an ingress envelope for intake registration.

    Args:
        envelope (dict[str, str]): The ingress envelope
        attachment_id (Optional[str]): The attachment id

    Returns:
        tuple[str, str, str] | None: request_id, attachment_id, blob_path
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
                blob_path = str(image.blob_path or "").strip()
                if not blob_path:
                    blob_path = normalize_image_blob_path(image.image_url)
                if not blob_path:
                    return None
                return (
                    str(envelope.get("request_id") or "").strip(),
                    image.attachment_id,
                    blob_path,
                )
        return None

    first = resolved_images[0]
    blob_path = str(first.blob_path or "").strip()
    if not blob_path:
        blob_path = normalize_image_blob_path(first.image_url)
    if not blob_path:
        return None
    return (
        str(envelope.get("request_id") or "").strip(),
        first.attachment_id,
        blob_path,
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
        categories: list[IntakeImageCategory] = []
        if not (
            cosmetic_pivot_enabled()
            and session.skin_snapshot_skipped
        ):
            categories.append(IntakeImageCategory.SKIN)
        categories.append(IntakeImageCategory.PRODUCT)
        return categories
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
        if (
            category == IntakeImageCategory.PRODUCT
            and text_product_review_eligible(session)
        ):
            if is_text_product_review_enqueued(_redis, session.case_id):
                continue
            return False
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
        if audit_type == IntakeImageCategory.PRODUCT:
            return True
        if audit_type == IntakeImageCategory.SKIN and _skin_prerequisite_enqueue_eligible(session):
            return True
        return False
    if intent == ServiceIntent.COMBINED:
        if (
            cosmetic_pivot_enabled()
            and session.skin_snapshot_skipped
            and audit_type == IntakeImageCategory.SKIN
        ):
            return False
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
        expected = 2
        if cosmetic_pivot_enabled() and session.skin_snapshot_skipped:
            expected = 1
        return session.case_id, expected
    return "", 0


def _truncate_audit_context(text: str, *, max_chars: int = MEDGEMMA_AUDIT_CONTEXT_MAX_CHARS) -> str:
    """
    Trim audit context to stay within MedGemma vLLM prompt budget.

    Args:
        text: Full audit context string.
        max_chars: Maximum allowed character length.

    Returns:
        str: Original text or a truncated variant with an explicit suffix.
    """
    normalized = str(text or "").strip()
    if len(normalized) <= max_chars:
        return normalized

    suffix = " [truncated for MedGemma context budget]"
    keep = max(0, max_chars - len(suffix))
    return f"{normalized[:keep].rstrip()}{suffix}"


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
    return _truncate_audit_context("\n".join(parts))


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

    if (
        session.service_intent == ServiceIntent.PRODUCT_REVIEW
        and audit_type == IntakeImageCategory.SKIN
        and session.pending_product_after_skin
    ):
        return (
            "Skin snapshot queued for product check. Product review will run automatically "
            "when skin analysis completes. Do NOT enqueue product yet. Tell the user their "
            "skin photo is being reviewed and the product check will follow."
        )

    return (
        "The image audit request is now in the queue. Do NOT call this tool again. "
        "Tell the user their photo was received and is being reviewed by the specialist team, "
        "and that results will follow shortly."
    )


def _resolve_image_blob_path_from_payload(payload_obj: dict[str, Any]) -> str:
    """
    Resolve the first durable client-media blob path from a payload object.

    Args:
        payload_obj: Parsed ingress payload.

    Returns:
        ``client-media/...`` path or empty string.
    """
    return resolve_first_image_blob_path_from_payload(payload_obj, redis_client=_redis)


def _resolve_intake_image_blob_path(
    image_ref: IntakeImageRef,
    payload_obj: dict[str, Any] | None = None,
) -> str:
    """
    Resolve the durable blob path for a registered intake image.

    Args:
        image_ref: Registered intake image reference.
        payload_obj: Optional ingress payload fallback.

    Returns:
        Normalized blob path or empty string.
    """
    blob_path = str(image_ref.image_blob_path or "").strip()
    if blob_path:
        return normalize_image_blob_path(blob_path) or blob_path
    blob_path = normalize_image_blob_path(str(image_ref.image_url or ""))
    if blob_path:
        return blob_path
    if payload_obj is not None:
        return _resolve_image_blob_path_from_payload(payload_obj)
    return ""


def _fresh_signed_url_for_blob_path(blob_path: str) -> str:
    """
    Mint a fresh signed URL for a client-media blob path.

    Args:
        blob_path: Durable Firebase object path.

    Returns:
        Signed HTTPS URL or empty string on failure.
    """
    normalized = str(blob_path or "").strip()
    if not normalized:
        return ""
    try:
        return sign_client_image_blob_path(normalized)
    except Exception:
        logger.exception("failed to sign intake image blob path: path=%s", normalized)
        return ""


def _dispatch_audit_image_ref(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    context: str,
    image_blob_path: str = "",
    image_url: str = "",
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
        context: Model context text.
        image_blob_path: Preferred durable Firebase blob path.
        image_url: Legacy signed URL fallback when blob path is unavailable.
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
        "context": context,
    }
    normalized_blob_path = normalize_image_blob_path(str(image_blob_path or "").strip())
    if normalized_blob_path:
        payload["image_blob_path"] = normalized_blob_path
    elif str(image_url or "").strip():
        payload["image_url"] = str(image_url).strip()
    else:
        raise ValueError("image_blob_path or image_url is required for audit dispatch")
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

    if response.status_code == 402:
        detail = "Insufficient credits for image analysis."
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = str(body["detail"])
        except Exception:
            pass
        return {
            "status": "insufficient_credits",
            "request_id": request_id,
            "endpoint": endpoint,
            "detail": detail,
            "instruction": (
                "The user does not have enough credits for MedGemma image analysis. "
                "Tell them to top up before retrying."
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
    Return the current UTC time once when needed for timestamps or scheduling.

    Do not call repeatedly in the same turn; one call is sufficient.
    """
    logger.info("TOOL CALL: get_time")
    return datetime.now(timezone.utc).isoformat()


@tool(args_schema=UpdateIntakeInput)
def update_intake(
    request_id: str,
    service_intent: Optional[ServiceIntent] = None,
    primary_concern: Optional[str] = None,
    consultation_notes: Optional[list[str]] = None,
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
    confirm_reproductive_status: Optional[bool] = None,
    skin_snapshot_skipped: Optional[bool] = None,
    personalization_mode: Optional[PersonalizationMode] = None,
    product_ref_text: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record any subset of intake facts in ONE call (load, mutate, recompute, save).

    Use this single tool for all structured intake instead of separate setters. Pass
    only the fields the user actually provided this turn; omitted fields are unchanged.

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator input.
        service_intent (enum, optional): skin_review, product_review, combined, or general.
        primary_concern (string, optional): User's main question in one short sentence.
        consultation_notes (list[str], optional): Short consultation facts to append
            (symptoms, timeline, routine changes) — not the main question or profile data.
        age, gender, location, skin_type, current_products, current_medications,
        current_conditions, current_goals, allergies, pregnancy, breastfeeding, notes
            (optional): Long-lived skin-profile fields; merged into the stored profile.
        confirm_reproductive_status (bool, optional): Confirm pregnancy/breastfeeding was
            discussed. Implied automatically when pregnancy or breastfeeding is provided.
        skin_snapshot_skipped (bool, optional): Set true only after the user explicitly
            declines a skin photo and accepts a general (non-tailored) product safety check.
            Automatically sets personalization_mode to generic. If switching from combined
            or skin_review to product-only, also pass service_intent=product_review.
        personalization_mode (enum, optional): Override personalization mode when needed.
        product_ref_text (string, optional): Product name or text when user provides it
            without a label photo (cosmetic pivot routing). Label photo still required for enqueue.

    Returns:
        Intake status JSON (phase, service_intent, images, profile_gaps, missing_for_routing,
        ready_to_route, collected_context_summary, next_recommended_tool, instruction) with a
        status of saved when something changed or unchanged when nothing did, plus updated_fields.

    Register images with register_intake_image; enqueue with enque_request_for_auditing_stream.
    """
    payload = UpdateIntakeInput(
        request_id=request_id,
        service_intent=service_intent,
        primary_concern=primary_concern,
        consultation_notes=consultation_notes,
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
        confirm_reproductive_status=confirm_reproductive_status,
        skin_snapshot_skipped=skin_snapshot_skipped,
        personalization_mode=personalization_mode,
        product_ref_text=product_ref_text,
    )

    logger.info(
        "TOOL CALL: update_intake: request_id=%s intent=%s profile_update=%s repro=%s",
        request_id,
        service_intent.value if service_intent else None,
        payload.has_profile_update(),
        payload.implies_reproductive_confirmation(),
    )

    envelope, error = _require_ingress_envelope(request_id)
    if error:
        return error

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or envelope.get("request_id") or request_id).strip()

    # 1. Persist skin-profile fields first so the intake recomputation below sees
    #    fresh profile gaps; fall back to the stored profile when nothing changed.
    if payload.has_profile_update():
        profile, profile_error = _persist_skin_profile(client_id, payload.to_profile_update())
        if profile_error:
            profile_error["request_id"] = request_id
            return profile_error
        assert profile is not None
    else:
        profile = _load_skin_profile_mapping(client_id)

    # 2. Load the intake session.
    try:
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)
    except IntakeSessionStoreError as exc:
        return {
            "status": "error",
            "detail": f"failed to load intake session: {exc}",
            "request_id": request_id,
        }

    # 3. Apply session-level mutations, tracking what actually changed.
    updates: dict[str, Any] = {}
    changed: list[str] = []

    if service_intent is not None and session.service_intent != service_intent:
        updates["service_intent"] = service_intent
        changed.append("service_intent")

    if primary_concern is not None:
        concern_text = primary_concern.strip()
        if concern_text and session.primary_concern.strip() != concern_text:
            updates["primary_concern"] = concern_text
            changed.append("primary_concern")

    if consultation_notes:
        new_notes = [note.strip() for note in consultation_notes if str(note).strip()]
        if new_notes:
            updates["consultation_notes"] = list(session.consultation_notes) + new_notes
            changed.append("consultation_notes")

    if payload.implies_reproductive_confirmation() and not session.reproductive_status_confirmed:
        updates["reproductive_status_confirmed"] = True
        changed.append("reproductive_status_confirmed")

    confirmed_none = payload.confirmed_none_profile_fields()
    if confirmed_none:
        merged_none = sorted(set(session.confirmed_none_fields) | set(confirmed_none))
        if merged_none != sorted(session.confirmed_none_fields):
            updates["confirmed_none_fields"] = merged_none
            changed.append("confirmed_none_fields")

    if payload.has_profile_update():
        changed.append("skin_profile")

    if skin_snapshot_skipped is not None and session.skin_snapshot_skipped != skin_snapshot_skipped:
        updates["skin_snapshot_skipped"] = skin_snapshot_skipped
        changed.append("skin_snapshot_skipped")
        if skin_snapshot_skipped:
            updates["personalization_mode"] = "generic"
            changed.append("personalization_mode")
        elif normalize_personalization_mode(
            str(updates.get("personalization_mode", session.personalization_mode))
        ) == "generic":
            updates["personalization_mode"] = "personalized"
            changed.append("personalization_mode")

    if personalization_mode is not None:
        normalized_mode = normalize_personalization_mode(personalization_mode)
        pending_mode = normalize_personalization_mode(
            str(updates.get("personalization_mode", session.personalization_mode))
        )
        if pending_mode != normalized_mode:
            updates["personalization_mode"] = normalized_mode
            if "personalization_mode" not in changed:
                changed.append("personalization_mode")

    if product_ref_text is not None:
        normalized_ref = product_ref_text.strip()
        if normalized_ref and session.product_ref_text.strip() != normalized_ref:
            updates["product_ref_text"] = normalized_ref
            changed.append("product_ref_text")

    if updates:
        session = session.model_copy(update=updates)

    # 4. Recompute derived fields and persist once.
    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "saved" if changed else "unchanged"
    result["updated_fields"] = changed
    return _merge_auto_dispatch_into_intake_result(
        result,
        client_id=client_id,
        request_id=request_id,
        source="update_intake",
    )


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

    Call once per image after visually classifying it. The returned status includes the
    next_recommended_tool and instruction to follow.

    When ready_to_route is true after registration, you MUST call enque_request_for_auditing_stream
    (audit_type skin or product per service_intent) before your final reply. The server may
    auto-dispatch if you skip this, but always follow the instruction field.
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

    image_request_id, resolved_attachment_id, image_blob_path = resolved
    signed_image_url = _fresh_signed_url_for_blob_path(image_blob_path)
    if not signed_image_url:
        return {
            "status": "error",
            "request_id": str(envelope.get("request_id") or request_id),
            "detail": "could not resolve a downloadable image URL for this request",
            "instruction": (
                "The image could not be resolved for storage. Ask the user to resend the photo."
            ),
        }
    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or image_request_id).strip()
    profile = _load_skin_profile_mapping(client_id)

    try:
        # Create a new intake session if one doesn't exist
        session = get_or_create_intake_session(client_id, case_id, redis_client=_redis)

    except IntakeSessionStoreError as exc:
        return {"status": "error", "detail": f"failed to load intake session: {exc}", "request_id": request_id}

    images = dict(session.images)
    images[image_category.value] = IntakeImageRef(
        request_id=image_request_id,
        image_url=signed_image_url,
        image_blob_path=image_blob_path,
        attachment_id=resolved_attachment_id,
        registered_at_ms=int(time.time() * 1000),
        visual_note=str(visual_note or "").strip(),
    )
    session_updates: dict[str, Any] = {"images": images}
    if (
        session.phase in (IntakePhase.AWAITING_AUDIT, IntakePhase.DELIVERED)
        and not session.pending_product_after_skin
        and not _is_audit_enqueued(image_request_id)
    ):
        # A brand-new (never dispatched) photo on a terminal-phase session means the
        # user is starting a fresh review; reopen so the audit can be enqueued.
        # infer_phase treats awaiting_audit/delivered as sticky, so without this
        # reset the new image would be reported as "already queued" forever.
        session_updates["phase"] = IntakePhase.IMAGE_INTAKE
        logger.info(
            "reopening intake session for new image: client_id=%s case_id=%s "
            "category=%s prior_phase=%s",
            client_id,
            case_id,
            image_category.value,
            session.phase.value,
        )
    session = session.model_copy(update=session_updates)

    updated, persist_error = _persist_intake_session(session, profile)
    if persist_error:
        return persist_error
    assert updated is not None

    result = _format_intake_status(updated, profile, request_id=request_id)
    result["status"] = "registered"
    result["registered_category"] = image_category.value
    result["registered_image_request_id"] = image_request_id
    record_image_anchor(
        _redis,
        client_id=client_id,
        category=image_category.value,
        anchor_request_id=image_request_id,
        case_id=case_id,
        message_id=str(envelope.get("message_id") or request_id),
        channel=str(envelope.get("channel") or ""),
        image_blob_path=image_blob_path,
    )
    return _merge_auto_dispatch_into_intake_result(
        result,
        client_id=client_id,
        request_id=request_id,
        source="register_intake_image",
    )


@tool
def enque_request_for_auditing_stream(
    request_id: OrchestratorRequestIdArg,
    audit_type: AuditTypeArg,
    collected_context: AuditCollectedContextArg = "",
) -> dict[str, Any]:
    """
    Send a registered intake image to the specialist audit team (MedGemma + Dermatologist).

    Arguments:
        request_id (string, required): Exact request_id from the current orchestrator turn
            (used to resolve client and intake session; not the image message id).
        audit_type (string, required): skin or product — which registered intake slot to enqueue.
        collected_context (string, optional): Extra short context; server merges intake summary.
            Default empty.

    Returns:
        JSON with status (queued, already_queued, blocked_not_ready, blocked_wrong_audit_type,
        blocked_missing_image, error), audit_type, image_request_id, instruction.

    Call only when intake shows ready_to_route true. For combined intent, call twice
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

    if session.phase == IntakePhase.AWAITING_AUDIT and (
        session.pending_product_after_skin or _all_required_audits_enqueued(session)
    ):
        return {
            "status": "already_queued",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "instruction": (
                "All required image audits were already dispatched. Do NOT call this tool again. "
                "Tell the user their photo(s) are being reviewed."
            ),
        }

    if not _ready_for_audit_enqueue(session, audit_type):
        return {
            "status": "blocked_not_ready",
            "request_id": request_id,
            "audit_type": audit_type.value,
            "missing_for_routing": list(session.missing_for_routing),
            "instruction": (
                "Intake is not ready to route. Complete the missing items with update_intake "
                "or register_intake_image, then enqueue."
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
        if (
            audit_type == IntakeImageCategory.PRODUCT
            and text_product_review_eligible(session)
        ):
            try:
                text_result = dispatch_text_product_review_signal(
                    _redis,
                    session=session,
                    profile=profile,
                    request_id=request_id,
                    case_id=case_id,
                    client_id=client_id,
                    message_id=str(envelope.get("message_id") or request_id),
                    channel=str(envelope.get("channel") or ""),
                    collected_context=collected_context,
                )
            except RuntimeError as exc:
                return {
                    "status": "error",
                    "request_id": request_id,
                    "audit_type": audit_type.value,
                    "detail": str(exc),
                }

            _emit_orchestrator_event(
                {
                    "request_id": request_id,
                    "case_id": case_id,
                    "client_id": client_id,
                    "message_id": str(envelope.get("message_id") or request_id),
                    "channel": str(envelope.get("channel") or ""),
                    "agent": "orchestrator",
                    "status": "routed_text_product_review",
                    "enqueue_source": "orchestrator_tool",
                    "audit_type": audit_type.value,
                    "product_ref_text": session.product_ref_text,
                }
            )

            if _all_required_audits_enqueued(session):
                _mark_intake_awaiting_audit(session, profile)

            text_result["request_id"] = request_id
            text_result["product_ref_text"] = session.product_ref_text
            instruction = _enqueue_audit_instruction(
                session,
                audit_type,
                str(text_result.get("status") or ""),
            )
            if instruction:
                text_result["instruction"] = instruction
            return text_result

        if (
            audit_type == IntakeImageCategory.PRODUCT
            and session.product_ref_text.strip()
            and cosmetic_pivot_enabled()
            and not cosmetic_text_product_review_enabled()
        ):
            return {
                "status": "blocked_missing_image",
                "request_id": request_id,
                "audit_type": audit_type.value,
                "product_ref_text": session.product_ref_text,
                "instruction": (
                    "Product name is recorded but a clear label photo is still required "
                    "for specialist review. Ask the user to send a readable product label image, "
                    "then register_intake_image before enqueueing."
                ),
            }
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

    try:
        payload_obj: dict[str, Any] = json.loads(str(image_envelope.get("payload") or "{}"))
    except Exception:
        payload_obj = {}

    image_blob_path = _resolve_intake_image_blob_path(image_ref, payload_obj)
    image_url = str(image_ref.image_url or "").strip()
    if not image_url and image_blob_path:
        image_url = _fresh_signed_url_for_blob_path(image_blob_path)
    if not image_url and not image_blob_path:
        image_url = _resolve_image_url_from_payload(payload_obj)
        if not image_blob_path:
            image_blob_path = _resolve_image_blob_path_from_payload(payload_obj)

    if not image_blob_path and not image_url:
        logger.info(
            "audit tool blocked (image unresolved): image_request_id=%s audit_type=%s",
            image_ref.request_id,
            audit_type.value,
        )
        return {
            "status": "blocked_missing_image",
            "request_id": request_id,
            "image_request_id": image_ref.request_id,
            "audit_type": audit_type.value,
            "instruction": (
                "Registered image could not be resolved. Ask the user to upload the image "
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
        if (
            audit_type == IntakeImageCategory.SKIN
            and _skin_prerequisite_enqueue_eligible(session)
        ):
            session = _mark_skin_prerequisite_pending(session, profile)
        elif _all_required_audits_enqueued(session):
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
            image_blob_path=image_blob_path,
            image_url=image_url if not image_blob_path else "",
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

    if result.get("status") in {"under_pressure", "insufficient_credits"}:
        if _redis is not None:
            try:
                _redis.delete(dedupe_key)
            except Exception:
                logger.exception(
                    "failed to clear dedupe key after non-success dispatch: image_request_id=%s",
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

    if (
        audit_type == IntakeImageCategory.SKIN
        and _skin_prerequisite_enqueue_eligible(session)
    ):
        session = _mark_skin_prerequisite_pending(session, profile)
    elif _all_required_audits_enqueued(session):
        _mark_intake_awaiting_audit(session, profile)

    result["request_id"] = request_id
    result["image_request_id"] = image_ref.request_id
    result["audit_type"] = audit_type.value
    instruction = _enqueue_audit_instruction(session, audit_type, str(result.get("status") or ""))
    if instruction:
        result["instruction"] = instruction
    return result


def continue_product_review_after_skin_prerequisite(
    client_id: str,
    case_id: str,
    *,
    request_id: str = "",
) -> bool:
    """
    Dispatch product review automatically after a skin prerequisite audit completes.

    Called from the orchestrator signal worker when ``pending_product_after_skin`` is set
    on the intake session and a skin review signal has been delivered.

    Args:
        client_id: Client identifier.
        case_id: Case identifier from the skin signal envelope.
        request_id: Request id from the skin signal (used for product dispatch).

    Returns:
        bool: True when product review was dispatched successfully.
    """
    cid = _normalize_client_id(client_id)
    case = str(case_id or "").strip()
    if not cid:
        return False

    try:
        session = load_intake_session(cid, redis_client=_redis)
    except IntakeSessionStoreError:
        logger.exception(
            "product auto-chain: failed to load intake session: client_id=%s",
            cid,
        )
        return False

    if session is None and case:
        session = load_archived_intake_session(case, redis_client=_redis)

    if session is None or not session.pending_product_after_skin:
        return False

    profile = _load_skin_profile_mapping(cid)
    reset_session = session.model_copy(
        update={
            "pending_product_after_skin": False,
            "phase": IntakePhase.GREETING,
        }
    )
    session = recompute_intake_session(reset_session, profile)

    if session.missing_for_routing:
        logger.warning(
            "product auto-chain blocked after skin prerequisite: client_id=%s missing=%s",
            cid,
            session.missing_for_routing,
        )
        try:
            save_intake_session_recomputed(session, profile, redis_client=_redis)
        except IntakeSessionStoreError:
            logger.exception(
                "product auto-chain: failed to persist blocked session: client_id=%s",
                cid,
            )
        return False

    req_id = str(request_id or case or cid).strip()
    dispatched = False

    if text_product_review_eligible(session):
        if is_text_product_review_enqueued(_redis, session.case_id):
            logger.info(
                "product auto-chain skipped; text review already enqueued: case_id=%s",
                session.case_id,
            )
            return False
        try:
            dispatch_text_product_review_signal(
                _redis,
                session=session,
                profile=profile,
                request_id=req_id,
                case_id=session.case_id,
                client_id=cid,
                message_id=req_id,
                channel="",
                collected_context=session.collected_context_summary,
            )
            dispatched = True
        except RuntimeError as exc:
            logger.warning(
                "product auto-chain text dispatch failed: client_id=%s err=%s",
                cid,
                exc,
            )
            return False
    else:
        product_ref = session.images.get(IntakeImageCategory.PRODUCT.value)
        if product_ref is None:
            logger.warning(
                "product auto-chain blocked; no product image registered: client_id=%s",
                cid,
            )
            try:
                save_intake_session_recomputed(session, profile, redis_client=_redis)
            except IntakeSessionStoreError:
                logger.exception(
                    "product auto-chain: failed to persist session: client_id=%s",
                    cid,
                )
            return False

        dedupe_key = _audit_enqueued_dedupe_key(product_ref.request_id)
        if _redis is not None and not _redis.set(
            dedupe_key, "1", nx=True, ex=_AUDIT_ENQUEUED_TTL_SEC
        ):
            logger.info(
                "product auto-chain skipped; product audit already dispatched: request_id=%s",
                product_ref.request_id,
            )
            return False

        image_envelope = _get_cached_ingress_envelope(product_ref.request_id)
        if not image_envelope:
            if _redis is not None:
                _redis.delete(dedupe_key)
            logger.warning(
                "product auto-chain blocked; ingress cache missing: image_request_id=%s",
                product_ref.request_id,
            )
            return False

        try:
            payload_obj: dict[str, Any] = json.loads(str(image_envelope.get("payload") or "{}"))
        except Exception:
            payload_obj = {}

        image_blob_path = _resolve_intake_image_blob_path(product_ref, payload_obj)
        image_url = str(product_ref.image_url or "").strip()
        if not image_url and image_blob_path:
            image_url = _fresh_signed_url_for_blob_path(image_blob_path)
        if not image_url and not image_blob_path:
            image_url = _resolve_image_url_from_payload(payload_obj)
            if not image_blob_path:
                image_blob_path = _resolve_image_blob_path_from_payload(payload_obj)

        if not image_blob_path and not image_url:
            if _redis is not None:
                _redis.delete(dedupe_key)
            logger.warning(
                "product auto-chain blocked; product image unresolved: request_id=%s",
                product_ref.request_id,
            )
            return False

        context_text = _build_audit_context_text(
            session,
            profile,
            IntakeImageCategory.PRODUCT,
            "",
        )
        try:
            _dispatch_audit_image_ref(
                request_id=product_ref.request_id,
                case_id=str(image_envelope.get("case_id") or session.case_id),
                client_id=str(image_envelope.get("client_id") or cid),
                message_id=str(image_envelope.get("message_id") or product_ref.request_id),
                channel=str(image_envelope.get("channel") or ""),
                image_blob_path=image_blob_path,
                image_url=image_url if not image_blob_path else "",
                context=context_text,
            )
            dispatched = True
        except Exception:
            if _redis is not None:
                _redis.delete(dedupe_key)
            logger.exception(
                "product auto-chain image dispatch failed: client_id=%s case_id=%s",
                cid,
                session.case_id,
            )
            return False

    if dispatched:
        _mark_intake_awaiting_audit(session, profile)
        logger.info(
            "product review auto-chained after skin prerequisite: client_id=%s case_id=%s",
            cid,
            session.case_id,
        )

    return dispatched


def _dispatch_required_audits_if_ready(
    client_id: str,
    request_id: str,
    *,
    source: str,
) -> list[dict[str, Any]]:
    """
    Enqueue every required audit when intake is ready but not yet awaiting_audit.

    Args:
        client_id: Client identifier for the intake session.
        request_id: Orchestrator ingress request id (current turn or image message).
        source: Caller label for logs (tool name or post-turn).

    Returns:
        list[dict[str, Any]]: Tool results for each dispatch attempted; empty when
        nothing needed dispatching.
    """
    cid = _normalize_client_id(client_id)
    rid = str(request_id or "").strip()
    if not cid or not rid:
        return []

    try:
        session = load_intake_session(cid, redis_client=_redis)
    except IntakeSessionStoreError:
        logger.exception(
            "audit auto-dispatch: failed to load intake session: client_id=%s source=%s",
            cid,
            source,
        )
        return []
    if session is None or session.phase == IntakePhase.DELIVERED:
        return []
    if session.phase == IntakePhase.AWAITING_AUDIT and (
        session.pending_product_after_skin or _all_required_audits_enqueued(session)
    ):
        # Only skip when the audits are verifiably dispatched (Redis dedupe markers)
        # or a product chain is intentionally waiting on the skin result. A sticky
        # awaiting_audit phase with un-dispatched images must NOT block dispatch.
        return []

    profile = _load_skin_profile_mapping(cid)
    session = recompute_intake_session(session, profile)
    if session.missing_for_routing:
        return []

    results: list[dict[str, Any]] = []
    for category in _required_audit_categories(session):
        image_ref = session.images.get(category.value)

        if (
            category == IntakeImageCategory.PRODUCT
            and image_ref is None
            and text_product_review_eligible(session)
        ):
            if is_text_product_review_enqueued(_redis, session.case_id):
                continue
        elif image_ref is not None and _is_audit_enqueued(image_ref.request_id):
            continue
        elif image_ref is None:
            continue

        logger.info(
            "deterministic audit dispatch (%s): client_id=%s case_id=%s audit_type=%s",
            source,
            cid,
            session.case_id,
            category.value,
        )
        results.append(
            enque_request_for_auditing_stream.func(
                request_id=rid,
                audit_type=category,
            )
        )
    return results


def _audit_dispatch_failure_item(dispatched: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Return the first non-success audit dispatch result, if any.

    Args:
        dispatched: Raw results from ``enque_request_for_auditing_stream.func``.

    Returns:
        dict[str, Any] | None: Failure payload to surface to the model.
    """
    for item in dispatched:
        status = str(item.get("status") or "")
        if status in {"insufficient_credits", "under_pressure", "error"}:
            return item
    return None


def _merge_auto_dispatch_into_intake_result(
    result: dict[str, Any],
    *,
    client_id: str,
    request_id: str,
    source: str,
) -> dict[str, Any]:
    """
    Auto-enqueue required audits when a tool left intake ready but not yet routed.

    Args:
        result: Tool response dict to augment.
        client_id: Client identifier.
        request_id: Ingress request id for enqueue resolution.
        source: Caller label for logs.

    Returns:
        dict[str, Any]: Updated tool result with refreshed intake status when dispatch ran.
    """
    if not result.get("ready_to_route"):
        return result
    if result.get("phase") == IntakePhase.DELIVERED.value:
        return result
    # awaiting_audit is NOT skipped here: _dispatch_required_audits_if_ready
    # verifies actual dedupe state and no-ops when everything is dispatched.

    dispatched = _dispatch_required_audits_if_ready(
        client_id,
        request_id,
        source=source,
    )
    if not dispatched:
        return result

    result["auto_dispatched"] = [str(item.get("status") or "") for item in dispatched]
    try:
        session = load_intake_session(client_id, redis_client=_redis)
    except IntakeSessionStoreError:
        return result
    if session is None:
        return result
    profile = _load_skin_profile_mapping(client_id)
    session = recompute_intake_session(session, profile)
    refreshed = _format_intake_status(session, profile, request_id=request_id)
    refreshed["auto_dispatched"] = result["auto_dispatched"]
    failure = _audit_dispatch_failure_item(dispatched)
    if failure is not None:
        refreshed["dispatch_status"] = str(failure.get("status") or "")
        if failure.get("detail"):
            refreshed["dispatch_detail"] = str(failure["detail"])
        if failure.get("instruction"):
            refreshed["instruction"] = str(failure["instruction"])
    for key in (
        "status",
        "registered_category",
        "registered_image_request_id",
        "updated_fields",
    ):
        if key in result:
            refreshed[key] = result[key]
    return refreshed


def ensure_ready_intake_dispatched(client_id: str, request_id: str) -> list[dict[str, Any]]:
    """
    Deterministically enqueue required audits when the model skipped the tool call.

    Called by the orchestrator worker after an agent turn ends. When the intake
    session is ready_to_route but not yet awaiting_audit, the model finished its
    turn without calling enque_request_for_auditing_stream (e.g. it claimed the
    case was sent without dispatching it). This dispatches every still-missing
    required audit; the tool's dedupe and phase checks make repeat calls no-ops.

    Args:
        client_id: Client identifier for the intake session.
        request_id: Current turn's ingress request id (must be in the ingress cache).

    Returns:
        list[dict[str, Any]]: Tool results for each dispatch attempted; empty when
        nothing needed dispatching.
    """
    results = _dispatch_required_audits_if_ready(
        client_id,
        request_id,
        source="post_turn",
    )
    if results:
        cid = _normalize_client_id(client_id)
        logger.warning(
            "post-turn deterministic audit dispatch (model ended turn without enqueue): "
            "client_id=%s request_id=%s statuses=%s",
            cid,
            request_id,
            [str(item.get("status") or "") for item in results],
        )
    return results


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


def _format_search_results(raw: str, *, label: str = "Search") -> str:
    """
    Convert compact Tavily JSON into readable text for the orchestrator agent.

    Passes through non-JSON strings unchanged (configuration notices, terminal
    failure directives, cache hits that are already formatted).

    Args:
        raw: Compact JSON from ``_format_tavily_results``, or a plain-text message.
        label: Human-readable source label (e.g. ``Internet``, ``Wikipedia``).

    Returns:
        str: Formatted multi-line summary with numbered results, URLs, and snippets.
    """
    text = str(raw or "").strip()
    if not text:
        return f"{label}: no results."

    # Pass through terminal directives and configuration errors unchanged.
    if not text.startswith("{"):
        return text

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text

    if not isinstance(payload, dict):
        return text

    query = str(payload.get("query") or "").strip()
    items = payload.get("results") or []
    if not isinstance(items, list) or not items:
        if query:
            return f'{label} query "{query}": no results found.'
        return f"{label}: no results found."

    lines: list[str] = []
    if query:
        lines.append(f"{label} results for: {query}")
    lines.append("")

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled").strip() or "Untitled"
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   Summary: {content}")
        lines.append("")

    formatted = "\n".join(lines).strip()
    if len(formatted) <= SEARCH_RESULT_TOTAL_CHARS:
        return formatted
    return formatted[:SEARCH_RESULT_TOTAL_CHARS] + "\n...<truncated>"


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
    return _format_search_results(
        _run_tavily_search(
            label="Wikipedia",
            query=query,
            max_results=max(1, min(max_results, 5)),
            include_domains=["wikipedia.org"],
        ),
        label="Wikipedia",
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
    return _format_search_results(
        _run_tavily_search(
            label="Internet",
            query=query,
            max_results=max(1, min(max_results, 3)),
            include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "mydawa.com"],
            search_depth="advanced",
        ),
        label="Internet",
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


@tool
def get_dermatologist_escalation_context(request_id: OrchestratorRequestIdArg) -> dict[str, Any]:
    """
    Read whether a human dermatologist handoff is allowed for this client.

    Human dermatologist escalation is separate from enque_request_for_auditing_stream
    (AI specialist team / MedGemma). A completed AI skin or product review is required
    before request_specialist_escalation can succeed, unless a consent offer is pending.

    Args:
        request_id: Exact request_id from the current orchestrator turn.

    Returns:
        dict[str, Any]: Readiness flags, cached review ids, and next-step instruction.
    """
    envelope, ingress_error = _require_ingress_envelope(request_id)
    if ingress_error:
        return ingress_error
    assert envelope is not None
    client_id = str(envelope.get("client_id") or "anonymous").strip()
    pending = has_pending_consent_escalation_offer(
        client_id,
        case_id=str(envelope.get("case_id") or "").strip() or None,
    )
    readiness = dermatologist_escalation_readiness(
        _redis,
        client_id=client_id,
        has_pending_consent_offer=pending,
    )
    if pending:
        instruction = (
            "User accepted or requested a dermatologist. Call request_specialist_escalation "
            "once with the current turn request_id. Do NOT call enque_request_for_auditing_stream."
        )
    elif readiness.get("ready_for_dermatologist"):
        instruction = (
            "AI review is complete. If the user wants a human dermatologist, call "
            "request_specialist_escalation once (current turn request_id). "
            "Do NOT use enqueue for dermatologist connection."
        )
    else:
        instruction = (
            "Complete AI review first: finish intake, then enque_request_for_auditing_stream "
            "for skin and/or product as needed. After the user receives that report, "
            "request_specialist_escalation connects a human dermatologist. "
            "Do NOT call request_specialist_escalation until ready_for_dermatologist is true."
        )
    return {
        "status": "ok",
        "request_id": request_id,
        "client_id": client_id,
        **readiness,
        "instruction": instruction,
    }


@tool
def request_specialist_escalation(request_id: OrchestratorRequestIdArg) -> dict[str, Any]:
    """
    Connect the user with a human dermatologist (not the AI audit enqueue path).

    First tries a cached watch-tier consent offer. Otherwise dispatches using the
    client's last completed AI skin or product review anchor.

    Args:
        request_id: Exact request_id from the current orchestrator turn.

    Returns:
        dict[str, Any]: Dispatch status and client-facing instruction.
    """
    logger.info("TOOL CALL: request_specialist_escalation: request_id=%s", request_id)

    if not cosmetic_consent_escalation_active():
        return {
            "status": "disabled",
            "request_id": request_id,
            "instruction": (
                "Consent-gated escalation is not enabled. Do NOT call this tool. "
                "Continue the conversation normally."
            ),
        }

    envelope, ingress_error = _require_ingress_envelope(request_id)
    if ingress_error:
        return ingress_error
    assert envelope is not None

    case_id = str(envelope.get("case_id") or "").strip()
    client_id = str(envelope.get("client_id") or "anonymous").strip()
    message_id = str(envelope.get("message_id") or request_id).strip()
    channel = str(envelope.get("channel") or "").strip()
    logger.info(
        "request_specialist_escalation resolved ingress: request_id=%s client_id=%s case_id=%s",
        request_id,
        client_id,
        case_id,
    )
    if not client_id:
        return {
            "status": "error",
            "request_id": request_id,
            "detail": "missing client_id in ingress envelope",
        }

    shortfall = job_credits_shortfall(BillingJobType.SPECIALIST_ESCALATION.value)
    if shortfall > 0:
        logger.info(
            "specialist escalation blocked for credits: client_id=%s case_id=%s short=%s",
            client_id,
            case_id,
            shortfall,
        )
        return {
            "status": "insufficient_credits",
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "shortfall_credits": shortfall,
            "instruction": (
                "The user cannot afford specialist review and no specialist has been "
                f"contacted. Tell them they need {shortfall} more credits and offer to "
                "top up. Do NOT call this tool again until they have topped up."
            ),
        }

    if has_pending_consent_escalation_offer(client_id, case_id=case_id or None):
        if not dispatch_consent_escalation(client_id, case_id=case_id or None):
            return {
                "status": "error",
                "request_id": request_id,
                "case_id": case_id,
                "client_id": client_id,
                "detail": "failed to dispatch consent escalation job",
            }
        record_job_charge(
            BillingJobType.SPECIALIST_ESCALATION.value,
            dedupe_on=f"{client_id}:{case_id}",
        )
        return {
            "status": "queued",
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "client_message": ESCALATION_CLIENT_MESSAGE,
            "instruction": (
                "Tell the user a skin specialist has been contacted and will follow up. "
                "Use the client_message text. Do NOT call this tool again for the same offer."
            ),
        }

    intake_session = load_intake_session(client_id, redis_client=_redis)
    profile = _load_skin_profile_mapping(client_id)
    dispatch_status = dispatch_user_requested_escalation(
        client_id,
        current_request_id=request_id,
        current_case_id=case_id,
        message_id=message_id,
        channel=channel,
        intake_session=intake_session,
        profile=profile,
    )

    if dispatch_status == "dispatched":
        record_job_charge(
            BillingJobType.SPECIALIST_ESCALATION.value,
            dedupe_on=f"{client_id}:{case_id}",
        )
        return {
            "status": "queued",
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "dispatch_path": "user_requested",
            "client_message": ESCALATION_CLIENT_MESSAGE,
            "instruction": (
                "Tell the user a skin specialist has been contacted and will follow up. "
                "Use the client_message text. Do NOT call this tool again for this request."
            ),
        }

    if dispatch_status == "already_dispatched":
        return {
            "status": "already_queued",
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "instruction": (
                "Specialist review was already queued for this client recently. "
                "Reassure the user and do NOT call this tool again."
            ),
        }

    if dispatch_status == "missing_context":
        readiness = dermatologist_escalation_readiness(
            _redis,
            client_id=client_id,
            has_pending_consent_offer=False,
        )
        last_mode = str(readiness.get("last_review_mode") or "").strip()
        if last_mode == "product_review":
            missing_status = "missing_product_review_context"
            missing_instruction = (
                "Product AI review must finish before a human dermatologist can be connected. "
                "Complete intake and enque_request_for_auditing_stream with audit_type=product, "
                "wait for the product report, then call request_specialist_escalation again."
            )
        else:
            missing_status = "missing_skin_context"
            missing_instruction = (
                "Skin AI review must finish before a human dermatologist can be connected. "
                "Complete intake, register a skin photo if needed, and "
                "enque_request_for_auditing_stream with audit_type=skin. "
                "After the skin report is delivered, call request_specialist_escalation again. "
                "Do NOT use enqueue to reach a dermatologist directly."
            )
        return {
            "status": missing_status,
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "requires_ai_review_first": True,
            "instruction": missing_instruction,
        }

    return {
        "status": "error",
        "request_id": request_id,
        "case_id": case_id,
        "client_id": client_id,
        "detail": "failed to dispatch user-requested escalation job",
    }


@tool
def return_to_mydawa_main_menu(request_id: OrchestratorRequestIdArg) -> dict[str, Any]:
    """
    Return the user to the MYDAWA WhatsApp Main Menu on Turn.io (not the Qutis mini-menu).

    Clears ``active_handler`` and starts the configured Main Menu journey. Use only after
    the user explicitly chooses the MYDAWA platform menu over the Qutis skincare options.

    Args:
        request_id: Exact request_id from the current orchestrator turn.

    Returns:
        dict[str, Any]: Turn API outcome and agent-facing instruction text.
    """
    logger.info("TOOL CALL: return_to_mydawa_main_menu: request_id=%s", request_id)

    envelope, ingress_error = _require_ingress_envelope(request_id)
    if ingress_error:
        return ingress_error
    assert envelope is not None

    channel = str(envelope.get("channel") or "").strip().lower()
    client_id = str(envelope.get("client_id") or "").strip()
    message_id = str(envelope.get("message_id") or "").strip() or None

    if channel != "whatsapp":
        return {
            "status": "unsupported_channel",
            "request_id": request_id,
            "channel": channel,
            "instruction": (
                "MYDAWA main menu exit is only supported on WhatsApp. "
                "Explain that and offer to continue helping with Qutis here."
            ),
        }

    if not client_id:
        return _ingress_error(request_id, "missing client_id in ingress envelope")

    turn_result = return_to_mydawa_main_menu_sync(wa_id=client_id, message_id=message_id)
    status = str(turn_result.get("status") or "error")

    if status == "ok":
        delete_intake_session(client_id)
        checkpoint_cleared = reset_orchestrator_thread_checkpoint(client_id)
        return {
            "status": "ok",
            "request_id": request_id,
            "client_id": client_id,
            "detail": turn_result.get("detail"),
            "checkpoint_reset": checkpoint_cleared,
            "instruction": (
                "Confirm in one or two short sentences that they are returning to the "
                "MYDAWA WhatsApp services menu (prescriptions, delivery, consultations, etc.). "
                "Do NOT call update_intake, register_intake_image, or enqueue tools this turn. "
                "Their next message will be handled by the MYDAWA menu on WhatsApp."
            ),
        }

    if status == "disabled":
        return {
            "status": "disabled",
            "request_id": request_id,
            "client_id": client_id,
            "instruction": (
                "MYDAWA menu exit is disabled in this environment. "
                "Apologize briefly and continue the Qutis conversation."
            ),
        }

    if status == "misconfigured":
        return {
            "status": "misconfigured",
            "request_id": request_id,
            "client_id": client_id,
            "instruction": (
                "The MYDAWA menu handoff is not configured. "
                "Tell the user support will help them reach the main menu and continue Qutis if needed."
            ),
        }

    return {
        "status": "error",
        "request_id": request_id,
        "client_id": client_id,
        "detail": turn_result.get("detail"),
        "instruction": (
            "The handoff to the MYDAWA menu did not complete. Apologize briefly, "
            "suggest trying again in a moment or typing menu once more, and do not enqueue audits."
        ),
    }


orchestrator_tools: list = [
    get_time,
    get_credit_balance,
    create_topup,
    skin_profile_is_available,
    get_skin_profile,
    update_intake,
    register_intake_image,
    enque_request_for_auditing_stream,
    get_dermatologist_escalation_context,
    request_specialist_escalation,
    return_to_mydawa_main_menu,
    read_status_of_a_case,
    check_medgemma_health,
    read_soul_md,
    wikipedia_search,
    internet_search,
]



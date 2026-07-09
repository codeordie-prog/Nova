"""
Author: Dermascan Team
Date: 2026-06-28
Licence: MIT

Orchestrator intake session specification: models, Redis key layout, routing rules,
and pure helpers used before image-audit enqueue.

An intake session is the per-client active case worksheet (Redis JSON) that tracks
service intent, categorized images (skin vs product), consultation context, and
readiness for routing. Population is explicit via orchestrator tools; the worker
only loads and injects a snapshot each turn.
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Any, Final, Mapping, Optional

from pydantic import BaseModel, Field

# --- Redis layout ----------------------------------------------------------------

_ENV_INTAKE_TTL_S: Final[str] = "INTAKE_SESSION_TTL_S"
_ENV_MIN_PROFILE_FIELDS: Final[str] = "INTAKE_MIN_PROFILE_FIELDS"

INTAKE_SESSION_KEY_PREFIX: Final[str] = "orchestrator:intake:client"
INTAKE_SESSION_ARCHIVE_KEY_PREFIX: Final[str] = "orchestrator:intake:archive:case"

_DEFAULT_JOB_TTL_S: Final[int] = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
INTAKE_SESSION_TTL_S: Final[int] = max(
    60,
    int(os.getenv(_ENV_INTAKE_TTL_S, str(_DEFAULT_JOB_TTL_S))),
)

_DEFAULT_MIN_PROFILE_FIELDS: Final[tuple[str, ...]] = ("age", "gender", "current_products","current_goals", "allergies", "location")

# List-valued skin-profile fields where "the user has none" is a valid, complete
# answer. An explicit empty list from the model records that confirmation on the
# session (confirmed_none_fields) so the gap check stops demanding the field.
LIST_PROFILE_FIELDS: Final[tuple[str, ...]] = (
    "current_products",
    "current_medications",
    "current_conditions",
    "current_goals",
    "allergies",
)


def _parse_min_profile_fields() -> tuple[str, ...]:
    """
    Parse minimum skin-profile field names required before routing.

    Returns:
        tuple[str, ...]: Field names on the skin profile document.
    """
    raw = os.getenv(_ENV_MIN_PROFILE_FIELDS, "").strip()
    if not raw:
        return _DEFAULT_MIN_PROFILE_FIELDS
    fields = tuple(part.strip() for part in raw.split(",") if part.strip())
    return fields if fields else _DEFAULT_MIN_PROFILE_FIELDS


MINIMUM_PROFILE_FIELDS: Final[tuple[str, ...]] = _parse_min_profile_fields()

# --- Enumerations ----------------------------------------------------------------

class ServiceIntent(str, Enum):
    """User-selected or inferred review service type."""

    SKIN_REVIEW = "skin_review"
    PRODUCT_REVIEW = "product_review"
    COMBINED = "combined"
    GENERAL = "general"


class IntakePhase(str, Enum):
    """High-level orchestration phase for the active intake session."""

    GREETING = "greeting"
    PROFILE_ONBOARDING = "profile_onboarding"
    IMAGE_INTAKE = "image_intake"
    CONSULTATION = "consultation"
    READY_TO_ROUTE = "ready_to_route"
    AWAITING_AUDIT = "awaiting_audit"
    DELIVERED = "delivered"


class IntakeImageCategory(str, Enum):
    """Categorized intake image slots on the session."""

    SKIN = "skin"
    PRODUCT = "product"


# Missing-for-routing tokens returned to the model and stored on the session.
MISSING_SERVICE_INTENT: Final[str] = "service_intent"
MISSING_SKIN_IMAGE: Final[str] = "skin_image"
MISSING_PRODUCT_IMAGE: Final[str] = "product_image"
MISSING_PRIMARY_CONCERN: Final[str] = "primary_concern"
MISSING_PROFILE_MINIMUM: Final[str] = "profile_minimum"
MISSING_REPRODUCTIVE_STATUS: Final[str] = "reproductive_status"

# Intents that require confirming pregnancy / breastfeeding status during intake.
REPRODUCTIVE_STATUS_INTENTS: Final[frozenset[ServiceIntent]] = frozenset(
    {
        ServiceIntent.SKIN_REVIEW,
        ServiceIntent.PRODUCT_REVIEW,
        ServiceIntent.COMBINED,
    }
)

# --- Routing requirements per service intent -------------------------------------

class RoutingRequirements(BaseModel):
    """
    Routing prerequisites for one service intent.

    Attributes:
        required_image_categories: Image slots that must be registered before enqueue.
        requires_primary_concern: Whether primary_concern must be non-empty.
        requires_minimum_profile: Whether minimum skin-profile fields must be filled.
        requires_reproductive_status: Whether pregnancy/breastfeeding must be confirmed.
    """

    required_image_categories: tuple[IntakeImageCategory, ...] = Field(default_factory=tuple)
    requires_primary_concern: bool = True
    requires_minimum_profile: bool = True
    requires_reproductive_status: bool = True


ROUTING_REQUIREMENTS: Final[dict[ServiceIntent, RoutingRequirements]] = {
    ServiceIntent.SKIN_REVIEW: RoutingRequirements(
        required_image_categories=(IntakeImageCategory.SKIN,),
        requires_primary_concern=True,
        requires_minimum_profile=True,
        requires_reproductive_status=True,
    ),
    ServiceIntent.PRODUCT_REVIEW: RoutingRequirements(
        required_image_categories=(IntakeImageCategory.PRODUCT,),
        requires_primary_concern=True,
        requires_minimum_profile=True,
        requires_reproductive_status=True,
    ),
    ServiceIntent.COMBINED: RoutingRequirements(
        required_image_categories=(
            IntakeImageCategory.SKIN,
            IntakeImageCategory.PRODUCT,
        ),
        requires_primary_concern=True,
        requires_minimum_profile=True,
        requires_reproductive_status=True,
    ),
    ServiceIntent.GENERAL: RoutingRequirements(
        required_image_categories=(),
        requires_primary_concern=False,
        requires_minimum_profile=False,
        requires_reproductive_status=False,
    ),
}


# --- Session models --------------------------------------------------------------

class IntakeImageRef(BaseModel):
    """
    One categorized image registered during intake.

    Attributes:
        request_id: Ingress request id that carried the image attachment.
        image_url: Resolved downloadable URL (set server-side, not by the model).
        attachment_id: Attachment id from ingress payload when available.
        registered_at_ms: Unix timestamp ms when the image was registered.
        visual_note: Non-diagnostic intake observation from the orchestrator.
    """

    request_id: str = Field(min_length=1)
    image_url: str = Field(min_length=1)
    attachment_id: str = ""
    registered_at_ms: int = 0
    visual_note: str = ""


class IntakeSession(BaseModel):
    """
    Active orchestrator intake worksheet for one client conversation.

    Stored at ``orchestrator:intake:client:{client_id}`` as JSON until the case
    is routed, delivered, or archived.
    """

    client_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    phase: IntakePhase = IntakePhase.GREETING
    service_intent: Optional[ServiceIntent] = None
    images: dict[str, IntakeImageRef] = Field(default_factory=dict)
    primary_concern: str = ""
    consultation_notes: list[str] = Field(default_factory=list)
    collected_context_summary: str = ""
    reproductive_status_confirmed: bool = False
    confirmed_none_fields: list[str] = Field(default_factory=list)
    profile_gaps: list[str] = Field(default_factory=list)
    missing_for_routing: list[str] = Field(default_factory=list)
    created_at_ms: int = 0
    updated_at_ms: int = 0


# --- Key builders ----------------------------------------------------------------

def intake_session_key(client_id: str) -> str:
    """
    Build Redis key for the active intake session scoped by client.

    Args:
        client_id: Normalized client identifier.

    Returns:
        str: Redis key string.
    """
    return f"{INTAKE_SESSION_KEY_PREFIX}:{client_id.strip()}"


def intake_session_archive_key(case_id: str) -> str:
    """
    Build Redis key for an archived intake session after delivery.

    Args:
        case_id: Case identifier.

    Returns:
        str: Redis key string.
    """
    return f"{INTAKE_SESSION_ARCHIVE_KEY_PREFIX}:{case_id.strip()}"


# --- Pure helpers ----------------------------------------------------------------

def _profile_field_is_gap(field_name: str, profile: Mapping[str, Any]) -> bool:
    """
    Return True when a profile field is considered missing for routing minimums.

    Args:
        field_name: Skin profile field name.
        profile: Skin profile mapping.

    Returns:
        bool: True when the field counts as a gap.
    """
    value = profile.get(field_name)
    if field_name in LIST_PROFILE_FIELDS:
        if not isinstance(value, list):
            return True
        return not any(str(item).strip() for item in value)
    if field_name in ("pregnancy", "breastfeeding"):
        return False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, int):
        return field_name == "age" and value <= 0
    if value is None:
        return True
    return False


def compute_profile_gaps(
    profile: Mapping[str, Any],
    confirmed_none_fields: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """
    List skin-profile field names still missing for routing minimums.

    Args:
        profile: Skin profile dict (e.g. from Firestore or SkinProfile.model_dump()).
        confirmed_none_fields: List-valued fields the user explicitly confirmed as
            "none"; these never count as gaps even when the stored list is empty.

    Returns:
        list[str]: Gap tokens matching MINIMUM_PROFILE_FIELDS names.
    """
    gaps: list[str] = []
    for field_name in MINIMUM_PROFILE_FIELDS:
        if field_name in confirmed_none_fields:
            continue
        if _profile_field_is_gap(field_name, profile):
            gaps.append(field_name)
    return gaps


def compute_missing_for_routing(
    session: IntakeSession,
    profile: Mapping[str, Any],
) -> list[str]:
    """
    Derive missing routing prerequisites from session state and profile.

    Args:
        session: Current intake session.
        profile: Current skin profile mapping.

    Returns:
        list[str]: Stable missing tokens (see MISSING_* constants).
    """
    missing: list[str] = []

    if session.service_intent is None:
        missing.append(MISSING_SERVICE_INTENT)
        return missing

    requirements = ROUTING_REQUIREMENTS[session.service_intent]

    for category in requirements.required_image_categories:
        if category.value not in session.images:
            if category == IntakeImageCategory.SKIN:
                missing.append(MISSING_SKIN_IMAGE)
            elif category == IntakeImageCategory.PRODUCT:
                missing.append(MISSING_PRODUCT_IMAGE)

    if requirements.requires_primary_concern and not session.primary_concern.strip():
        missing.append(MISSING_PRIMARY_CONCERN)

    if requirements.requires_minimum_profile:
        profile_gaps = compute_profile_gaps(profile, session.confirmed_none_fields)
        if profile_gaps:
            missing.append(MISSING_PROFILE_MINIMUM)

    if (
        requirements.requires_reproductive_status
        and session.service_intent in REPRODUCTIVE_STATUS_INTENTS
        and not session.reproductive_status_confirmed
    ):
        missing.append(MISSING_REPRODUCTIVE_STATUS)

    return missing


def infer_phase(session: IntakeSession, missing_for_routing: list[str]) -> IntakePhase:
    """
    Infer orchestration phase from session and computed missing tokens.

    Terminal phases ``awaiting_audit`` and ``delivered`` are preserved.

    Args:
        session: Current intake session.
        missing_for_routing: Output of ``compute_missing_for_routing``.

    Returns:
        IntakePhase: Suggested phase for injection and tooling.
    """
    if session.phase in (IntakePhase.AWAITING_AUDIT, IntakePhase.DELIVERED):
        return session.phase

    if not missing_for_routing:
        return IntakePhase.READY_TO_ROUTE

    if MISSING_SERVICE_INTENT in missing_for_routing:
        return IntakePhase.GREETING

    if MISSING_PROFILE_MINIMUM in missing_for_routing:
        return IntakePhase.PROFILE_ONBOARDING

    if MISSING_SKIN_IMAGE in missing_for_routing or MISSING_PRODUCT_IMAGE in missing_for_routing:
        return IntakePhase.IMAGE_INTAKE

    if (
        MISSING_PRIMARY_CONCERN in missing_for_routing
        or MISSING_REPRODUCTIVE_STATUS in missing_for_routing
    ):
        return IntakePhase.CONSULTATION

    return IntakePhase.CONSULTATION


def recompute_session(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    now_ms: Optional[int] = None,
) -> IntakeSession:
    """
    Refresh derived intake fields after a mutation.

    Args:
        session: Intake session to update in place logically.
        profile: Current skin profile mapping.
        now_ms: Optional timestamp override for tests.

    Returns:
        IntakeSession: New session instance with gaps, missing, and phase updated.
    """
    timestamp = now_ms if now_ms is not None else int(time.time() * 1000)
    profile_gaps = compute_profile_gaps(profile, session.confirmed_none_fields)
    missing = compute_missing_for_routing(session, profile)
    phase = infer_phase(session, missing)

    updates: dict[str, Any] = {
        "profile_gaps": profile_gaps,
        "missing_for_routing": missing,
        "phase": phase,
        "updated_at_ms": timestamp,
    }
    if session.created_at_ms <= 0:
        updates["created_at_ms"] = timestamp

    return session.model_copy(update=updates)


def is_ready_to_route(session: IntakeSession, profile: Mapping[str, Any]) -> bool:
    """
    Return True when all routing prerequisites are satisfied.

    Args:
        session: Current intake session.
        profile: Current skin profile mapping.

    Returns:
        bool: True when ``missing_for_routing`` would be empty.
    """
    return not compute_missing_for_routing(session, profile)


def build_collected_context_summary(
    session: IntakeSession,
    profile: Mapping[str, Any],
) -> str:
    """
    Build a deterministic audit context string from structured intake state.

    Args:
        session: Current intake session.
        profile: Current skin profile mapping.

    Returns:
        str: Compact summary for MedGemma enqueue and delivery shaping.
    """
    intent_label = session.service_intent.value if session.service_intent else "unknown"
    parts: list[str] = [f"Service: {intent_label}."]

    if session.primary_concern.strip():
        parts.append(f"Primary concern: {session.primary_concern.strip()}.")

    goals = profile.get("current_goals") or []
    if isinstance(goals, list) and goals:
        parts.append(f"Goals: {', '.join(str(g) for g in goals if str(g).strip())}.")

    allergies = profile.get("allergies") or []
    if isinstance(allergies, list) and allergies:
        parts.append(f"Allergies: {', '.join(str(a) for a in allergies if str(a).strip())}.")

    if session.reproductive_status_confirmed:
        pregnancy = profile.get("pregnancy")
        breastfeeding = profile.get("breastfeeding")
        if pregnancy or breastfeeding:
            status_bits: list[str] = []
            if pregnancy:
                status_bits.append("pregnant")
            if breastfeeding:
                status_bits.append("breastfeeding")
            parts.append(f"Reproductive status: {', '.join(status_bits)}.")
        else:
            parts.append("Reproductive status: not pregnant or breastfeeding.")

    notes = [str(note).strip() for note in session.consultation_notes if str(note).strip()]
    if notes:
        parts.append(f"Consultation notes: {' | '.join(notes)}.")

    for category, image_ref in session.images.items():
        note = image_ref.visual_note.strip()
        if note:
            parts.append(f"{category} image note: {note}.")

    return " ".join(parts).strip()


def new_intake_session(client_id: str, case_id: str) -> IntakeSession:
    """
    Create an empty intake session for a client and case.

    Args:
        client_id: Normalized client identifier.
        case_id: Active case identifier.

    Returns:
        IntakeSession: Fresh session with timestamps set.
    """
    now_ms = int(time.time() * 1000)
    return IntakeSession(
        client_id=client_id.strip(),
        case_id=case_id.strip(),
        phase=IntakePhase.GREETING,
        created_at_ms=now_ms,
        updated_at_ms=now_ms,
    )

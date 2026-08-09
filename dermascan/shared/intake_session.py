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

from dermascan.shared.cosmetic_types import PersonalizationMode

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
MISSING_SKIN_SNAPSHOT: Final[str] = "skin_snapshot_stale"
MISSING_MEDICATIONS_CONTEXT: Final[str] = "medications_context"

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
    skin_snapshot_skipped: bool = False
    personalization_mode: PersonalizationMode = "personalized"
    product_ref_text: str = ""
    pending_product_after_skin: bool = False
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


def _resolve_cosmetic_pivot_routing_enabled(cosmetic_pivot_enabled: bool | None) -> bool:
    """
    Resolve whether cosmetic pivot routing rules apply.

    Args:
        cosmetic_pivot_enabled: Explicit override for tests; when None, read env flag.

    Returns:
        bool: True when relaxed product routing is active.
    """
    if cosmetic_pivot_enabled is not None:
        return cosmetic_pivot_enabled
    from dermascan.shared.cosmetic_feature_flags import cosmetic_pivot_enabled as pivot_flag

    return pivot_flag()


def _resolve_skin_first_routing_enabled(cosmetic_skin_first_routing: bool | None) -> bool:
    """
    Resolve whether skin-first product routing rules apply.

    Args:
        cosmetic_skin_first_routing: Explicit override for tests; when None, read env flags.

    Returns:
        bool: True when pivot and skin-first routing are both active.
    """
    if cosmetic_skin_first_routing is not None:
        return cosmetic_skin_first_routing
    from dermascan.shared.cosmetic_feature_flags import cosmetic_skin_first_routing_active

    return cosmetic_skin_first_routing_active()


def _resolve_consent_escalation_routing_enabled(
    cosmetic_consent_escalation_active: bool | None,
) -> bool:
    """
    Resolve whether consent-gated escalation intake rules apply.

    Args:
        cosmetic_consent_escalation_active: Explicit override for tests.

    Returns:
        bool: True when medications context is required before skin enqueue.
    """
    if cosmetic_consent_escalation_active is not None:
        return cosmetic_consent_escalation_active
    from dermascan.shared.cosmetic_feature_flags import cosmetic_consent_escalation_active as flag

    return flag()


def _requires_medications_context_for_skin_audit(session: IntakeSession) -> bool:
    """
    Return True when a skin audit enqueue should include treatment/medication context.

    Args:
        session: Current intake session.

    Returns:
        bool: True for skin-review paths that feed consent escalation routing.
    """
    if session.service_intent == ServiceIntent.SKIN_REVIEW:
        return True
    if session.service_intent == ServiceIntent.COMBINED and not session.skin_snapshot_skipped:
        return True
    if (
        session.service_intent == ServiceIntent.PRODUCT_REVIEW
        and not session.skin_snapshot_skipped
    ):
        return True
    return False


def _medications_context_is_gap(
    profile: Mapping[str, Any],
    confirmed_none_fields: tuple[str, ...] | list[str],
) -> bool:
    """
    Return True when current medications are unknown for audit context.

    Args:
        profile: Skin profile mapping.
        confirmed_none_fields: Fields explicitly confirmed as none by the user.

    Returns:
        bool: True when medications must still be collected or confirmed none.
    """
    if "current_medications" in confirmed_none_fields:
        return False
    return _profile_field_is_gap("current_medications", profile)


def _format_profile_list_summary(
    field_name: str,
    label: str,
    profile: Mapping[str, Any],
    confirmed_none_fields: tuple[str, ...] | list[str],
) -> str | None:
    """
    Format a list-valued profile field for audit context summaries.

    Args:
        field_name: Skin profile field name.
        label: Human-readable label prefix.
        profile: Skin profile mapping.
        confirmed_none_fields: Fields explicitly confirmed as none by the user.

    Returns:
        str | None: Summary fragment, or None when the field is unset.
    """
    if field_name in confirmed_none_fields:
        return f"{label}: none."
    value = profile.get(field_name)
    if not isinstance(value, list):
        return None
    items = [str(item).strip() for item in value if str(item).strip()]
    if items:
        return f"{label}: {', '.join(items)}."
    return None


def has_product_image(session: IntakeSession) -> bool:
    """
    Return True when a product-category intake image is registered.

    Args:
        session: Current intake session.

    Returns:
        bool: True when product image slot is filled.
    """
    return IntakeImageCategory.PRODUCT.value in session.images


def has_product_reference(session: IntakeSession) -> bool:
    """
    Return True when the session has a product image or text reference.

    Args:
        session: Current intake session.

    Returns:
        bool: True when product image or ``product_ref_text`` is present.
    """
    return has_product_image(session) or bool(session.product_ref_text.strip())


def compute_missing_for_routing(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    cosmetic_pivot_enabled: bool | None = None,
    cosmetic_skin_first_routing: bool | None = None,
    cosmetic_consent_escalation_active: bool | None = None,
    skin_audit: Mapping[str, Any] | None = None,
    skin_audit_updated_at_ts: int | None = None,
    now_ts: int | None = None,
) -> list[str]:
    """
    Derive missing routing prerequisites from session state and profile.

    Args:
        session: Current intake session.
        profile: Current skin profile mapping.
        cosmetic_pivot_enabled: Optional override for pivot routing rules.
        cosmetic_skin_first_routing: Optional override for skin-first product gating.
        cosmetic_consent_escalation_active: Optional override for medications intake gate.
        skin_audit: Latest persisted skin audit for the client, if loaded.
        skin_audit_updated_at_ts: Redis envelope timestamp for the skin audit.
        now_ts: Optional unix timestamp override for freshness checks.

    Returns:
        list[str]: Stable missing tokens (see MISSING_* constants).
    """
    missing: list[str] = []

    if session.service_intent is None:
        missing.append(MISSING_SERVICE_INTENT)
        return missing

    pivot_routing = _resolve_cosmetic_pivot_routing_enabled(cosmetic_pivot_enabled)
    skin_first = _resolve_skin_first_routing_enabled(cosmetic_skin_first_routing)
    consent_escalation = _resolve_consent_escalation_routing_enabled(
        cosmetic_consent_escalation_active
    )
    requirements = ROUTING_REQUIREMENTS[session.service_intent]

    for category in requirements.required_image_categories:
        if category == IntakeImageCategory.SKIN:
            if (
                pivot_routing
                and session.skin_snapshot_skipped
                and session.service_intent == ServiceIntent.COMBINED
            ):
                continue
            if category.value not in session.images:
                missing.append(MISSING_SKIN_IMAGE)
        elif category == IntakeImageCategory.PRODUCT:
            if pivot_routing:
                if not has_product_reference(session):
                    missing.append(MISSING_PRODUCT_IMAGE)
            elif category.value not in session.images:
                missing.append(MISSING_PRODUCT_IMAGE)

    if requirements.requires_primary_concern and not session.primary_concern.strip():
        missing.append(MISSING_PRIMARY_CONCERN)

    if requirements.requires_minimum_profile:
        profile_gaps = compute_profile_gaps(profile, session.confirmed_none_fields)
        if profile_gaps:
            missing.append(MISSING_PROFILE_MINIMUM)

    if (
        consent_escalation
        and pivot_routing
        and _requires_medications_context_for_skin_audit(session)
        and _medications_context_is_gap(profile, session.confirmed_none_fields)
    ):
        missing.append(MISSING_MEDICATIONS_CONTEXT)

    if (
        requirements.requires_reproductive_status
        and session.service_intent in REPRODUCTIVE_STATUS_INTENTS
        and not session.reproductive_status_confirmed
    ):
        missing.append(MISSING_REPRODUCTIVE_STATUS)

    if (
        skin_first
        and pivot_routing
        and session.service_intent == ServiceIntent.PRODUCT_REVIEW
        and not session.skin_snapshot_skipped
    ):
        from dermascan.shared.cosmetic_profile import is_skin_snapshot_fresh

        if not is_skin_snapshot_fresh(
            skin_audit,
            skin_audit_updated_at_ts,
            now_ts=now_ts,
        ):
            missing.append(MISSING_SKIN_SNAPSHOT)

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

    if MISSING_MEDICATIONS_CONTEXT in missing_for_routing:
        return IntakePhase.PROFILE_ONBOARDING

    if MISSING_SKIN_IMAGE in missing_for_routing or MISSING_PRODUCT_IMAGE in missing_for_routing:
        return IntakePhase.IMAGE_INTAKE

    if MISSING_SKIN_SNAPSHOT in missing_for_routing:
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
    cosmetic_pivot_enabled: bool | None = None,
    cosmetic_skin_first_routing: bool | None = None,
    cosmetic_consent_escalation_active: bool | None = None,
    skin_audit: Mapping[str, Any] | None = None,
    skin_audit_updated_at_ts: int | None = None,
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
    profile_gaps = list(compute_profile_gaps(profile, session.confirmed_none_fields))
    now_ts = timestamp // 1000
    missing = compute_missing_for_routing(
        session,
        profile,
        cosmetic_pivot_enabled=cosmetic_pivot_enabled,
        cosmetic_skin_first_routing=cosmetic_skin_first_routing,
        cosmetic_consent_escalation_active=cosmetic_consent_escalation_active,
        skin_audit=skin_audit,
        skin_audit_updated_at_ts=skin_audit_updated_at_ts,
        now_ts=now_ts,
    )
    if (
        MISSING_MEDICATIONS_CONTEXT in missing
        and "current_medications" not in profile_gaps
    ):
        profile_gaps.append("current_medications")
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


def is_ready_to_route(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    cosmetic_pivot_enabled: bool | None = None,
    cosmetic_skin_first_routing: bool | None = None,
    cosmetic_consent_escalation_active: bool | None = None,
    skin_audit: Mapping[str, Any] | None = None,
    skin_audit_updated_at_ts: int | None = None,
    now_ts: int | None = None,
) -> bool:
    """
    Return True when all routing prerequisites are satisfied.

    Args:
        session: Current intake session.
        profile: Current skin profile mapping.
        cosmetic_pivot_enabled: Optional override for cosmetic pivot routing rules.
        cosmetic_skin_first_routing: Optional override for skin-first product gating.
        cosmetic_consent_escalation_active: Optional override for medications intake gate.
        skin_audit: Latest persisted skin audit for the client, if loaded.
        skin_audit_updated_at_ts: Redis envelope timestamp for the skin audit.
        now_ts: Optional unix timestamp override for freshness checks.

    Returns:
        bool: True when ``missing_for_routing`` would be empty.
    """
    return not compute_missing_for_routing(
        session,
        profile,
        cosmetic_pivot_enabled=cosmetic_pivot_enabled,
        cosmetic_skin_first_routing=cosmetic_skin_first_routing,
        cosmetic_consent_escalation_active=cosmetic_consent_escalation_active,
        skin_audit=skin_audit,
        skin_audit_updated_at_ts=skin_audit_updated_at_ts,
        now_ts=now_ts,
    )


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
    confirmed_none = session.confirmed_none_fields

    if session.primary_concern.strip():
        parts.append(f"Primary concern: {session.primary_concern.strip()}.")

    if session.product_ref_text.strip():
        parts.append(f"Product reference: {session.product_ref_text.strip()}.")

    if session.skin_snapshot_skipped:
        parts.append("Skin snapshot: skipped (generic product check).")

    skin_type = str(profile.get("skin_type") or "").strip()
    if skin_type:
        parts.append(f"Skin type: {skin_type}.")

    for field_name, label in (
        ("current_products", "Current products"),
        ("current_medications", "Current medications"),
        ("current_conditions", "Current conditions"),
    ):
        line = _format_profile_list_summary(field_name, label, profile, confirmed_none)
        if line:
            parts.append(line)

    goals = profile.get("current_goals") or []
    if isinstance(goals, list) and goals:
        parts.append(f"Goals: {', '.join(str(g) for g in goals if str(g).strip())}.")

    allergies = profile.get("allergies") or []
    if isinstance(allergies, list) and allergies:
        parts.append(f"Allergies: {', '.join(str(a) for a in allergies if str(a).strip())}.")

    profile_notes = str(profile.get("notes") or "").strip()
    if profile_notes:
        parts.append(f"Profile notes: {profile_notes}.")

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

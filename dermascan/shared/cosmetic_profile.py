"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
Licence: PolyForm Noncommercial License 1.0.0

Pure helpers for cosmetic profile state and personalization mode derivation.

No I/O — safe to unit test in isolation. Production callers wire Redis/Firestore
data into these functions in later pivot steps.
"""


from __future__ import annotations

import os
import time
from typing import Any, Mapping

from dermascan.shared.cosmetic_types import PersonalizationMode, normalize_personalization_mode

SKIN_SNAPSHOT_FRESHNESS_DAYS: int = max(
    1,
    int(os.getenv("SKIN_SNAPSHOT_FRESHNESS_DAYS", "60")),
)
SKIN_SNAPSHOT_FRESHNESS_SECONDS: int = SKIN_SNAPSHOT_FRESHNESS_DAYS * 24 * 3600


def _skin_audit_has_observations(skin_audit: Mapping[str, Any]) -> bool:
    """
    Return True when a skin audit dict contains usable vision-derived observations.

    Args:
        skin_audit: Skin audit mapping (e.g. from Redis ``latest_skin_audit``).

    Returns:
        bool: True when primary concern or condition fields are populated.
    """
    if not skin_audit:
        return False

    primary_concern = str(skin_audit.get("primary_concern") or "").strip()
    if primary_concern and primary_concern.lower() != "uncertain":
        return True

    condition = skin_audit.get("current_condition")
    if isinstance(condition, Mapping):
        severity = str(condition.get("severity") or "").strip().lower()
        barrier_status = str(condition.get("barrier_status") or "").strip().lower()
        patterns = condition.get("observed_patterns")
        if severity and severity != "unknown":
            return True
        if barrier_status and barrier_status != "unknown":
            return True
        if isinstance(patterns, list) and any(str(item).strip() for item in patterns):
            return True

    return False


def _minimum_profile_filled(profile: Mapping[str, Any]) -> bool:
    """
    Return True when enough text profile context exists for partial personalization.

    Args:
        profile: Skin profile mapping from Firestore/cache.

    Returns:
        bool: True when at least one constraint or goal field is non-empty.
    """
    if not profile:
        return False

    list_fields = ("current_goals", "allergies", "current_products")
    for field in list_fields:
        value = profile.get(field)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True

    scalar_fields = ("skin_type", "pregnancy", "breastfeeding")
    for field in scalar_fields:
        if str(profile.get(field) or "").strip():
            return True

    return False


def resolve_skin_snapshot_timestamp(
    skin_audit: Mapping[str, Any] | None,
    updated_at_ts: int | None,
) -> int | None:
    """
    Resolve the best available snapshot timestamp from audit payload or envelope.

    Args:
        skin_audit: Persisted skin audit mapping.
        updated_at_ts: Envelope ``updated_at_ts`` from Redis, when available.

    Returns:
        int | None: Unix timestamp seconds, or None when unknown.
    """
    if updated_at_ts is not None:
        try:
            return int(updated_at_ts)
        except (TypeError, ValueError):
            pass
    audit = skin_audit or {}
    raw = audit.get("snapshot_at_ts")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_skin_snapshot_fresh(
    skin_audit: Mapping[str, Any] | None,
    updated_at_ts: int | None,
    *,
    now_ts: int | None = None,
) -> bool:
    """
    Return True when a persisted skin audit exists and is within the freshness window.

    Args:
        skin_audit: Latest skin audit mapping from Redis.
        updated_at_ts: Snapshot envelope timestamp from Redis.
        now_ts: Optional current unix timestamp override for tests.

    Returns:
        bool: True when observations exist and the snapshot is not stale.
    """
    audit = skin_audit or {}
    if not _skin_audit_has_observations(audit):
        return False
    snapshot_ts = resolve_skin_snapshot_timestamp(audit, updated_at_ts)
    if snapshot_ts is None:
        return False
    current = int(now_ts if now_ts is not None else time.time())
    age_seconds = max(0, current - snapshot_ts)
    return age_seconds <= SKIN_SNAPSHOT_FRESHNESS_SECONDS


def derive_personalization_mode(
    *,
    skin_audit: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    skin_snapshot_skipped: bool = False,
    snapshot_fresh: bool = True,
) -> PersonalizationMode:
    """
    Derive how strongly product guidance may be tailored to the user.

    Priority:
    1. User opted out of skin snapshot → ``generic``
    2. Fresh skin audit with observations → ``personalized``
    3. Minimum text profile present → ``profile_only``
    4. Otherwise → ``generic``

    Args:
        skin_audit: Latest vision-derived skin audit, if any.
        profile: User skin profile (goals, allergies, routine, etc.).
        skin_snapshot_skipped: True when the user declined a skin photo.
        snapshot_fresh: False when an existing snapshot is stale (future routing use).

    Returns:
        PersonalizationMode: Mode for rules-engine product review.
    """
    if skin_snapshot_skipped:
        return "generic"

    audit = skin_audit or {}
    if snapshot_fresh and _skin_audit_has_observations(audit):
        return "personalized"

    if _minimum_profile_filled(profile or {}):
        return "profile_only"

    return "generic"


def resolve_product_personalization_mode(
    *,
    skin_audit: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    intake_session: Mapping[str, Any] | None = None,
    snapshot_fresh: bool = True,
) -> PersonalizationMode:
    """
    Resolve personalization mode for a product rules-engine request.

    Reads ``skin_snapshot_skipped`` from the intake session (when present) and
    delegates to :func:`derive_personalization_mode`. When the session stores an
    explicit non-default ``personalization_mode`` (e.g. from ``update_intake``),
    that value is respected so agent opt-out is not overridden by stale audits.

    Args:
        skin_audit: Latest vision-derived skin audit for the client.
        profile: User skin profile mapping.
        intake_session: Active or archived intake session dict, if available.
        snapshot_fresh: Whether the skin snapshot is within the freshness window.

    Returns:
        PersonalizationMode: Mode to attach to ``RulesEngineRequest``.
    """
    session = intake_session or {}
    skipped = bool(session.get("skin_snapshot_skipped"))

    stored_raw = str(session.get("personalization_mode") or "").strip()
    if stored_raw:
        stored_mode = normalize_personalization_mode(stored_raw)
        if skipped or stored_mode != "personalized":
            return stored_mode

    return derive_personalization_mode(
        skin_audit=skin_audit,
        profile=profile,
        skin_snapshot_skipped=skipped,
        snapshot_fresh=snapshot_fresh,
    )

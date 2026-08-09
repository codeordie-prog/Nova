"""
Author: Dermascan Team
Date: 2026-06-28
Licence: MIT

Redis persistence for orchestrator intake sessions.

Loads and saves ``IntakeSession`` documents at the keys defined in
``intake_session.py``. Callers may inject a Redis client (tests) or rely on
``REDIS_URL`` via the shared resilient client.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Final, Mapping, Optional

from dermascan.shared.cosmetic_feature_flags import (
    cosmetic_consent_escalation_active,
    cosmetic_pivot_enabled,
    cosmetic_skin_first_routing_active,
)
from dermascan.shared.intake_session import (
    INTAKE_SESSION_TTL_S,
    IntakePhase,
    IntakeSession,
    build_collected_context_summary,
    intake_session_archive_key,
    intake_session_key,
    new_intake_session,
    recompute_session,
)
from dermascan.shared.skin_audit_store import load_latest_skin_audit
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

_LOGGER: Final[logging.Logger] = logging.getLogger("dermascan.intake_session_store")


class IntakeSessionStoreError(RuntimeError):
    """Raised when intake session persistence fails after retries."""


def _resolve_redis(redis_client: Optional[Any]) -> Optional[Any]:
    """
    Return an explicit Redis client or build one from ``REDIS_URL``.

    Args:
        redis_client: Optional caller-provided client.

    Returns:
        Optional[Redis]: Connected client, or None when Redis is not configured.
    """
    if redis_client is not None:
        return redis_client
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    return get_redis_client(decode_responses=True)


def serialize_intake_session(session: IntakeSession) -> str:
    """
    Serialize an intake session to JSON for Redis storage.

    Args:
        session: Session to encode.

    Returns:
        str: UTF-8 JSON string.
    """
    return json.dumps(session.model_dump(mode="json"), default=str)


def deserialize_intake_session(raw: Any) -> Optional[IntakeSession]:
    """
    Parse a Redis payload into an ``IntakeSession``.

    Args:
        raw: Redis value (str, bytes, or dict).

    Returns:
        Optional[IntakeSession]: Parsed session, or None when invalid.
    """
    if raw is None:
        return None
    if isinstance(raw, IntakeSession):
        return raw
    if isinstance(raw, dict):
        try:
            return IntakeSession.model_validate(raw)
        except Exception:
            _LOGGER.exception("failed to validate intake session dict")
            return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        _LOGGER.exception("failed to decode intake session json")
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return IntakeSession.model_validate(payload)
    except Exception:
        _LOGGER.exception("failed to validate intake session payload")
        return None


def load_intake_session(
    client_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> Optional[IntakeSession]:
    """
    Load the active intake session for a client.

    Args:
        client_id: Client identifier.
        redis_client: Optional Redis client override.

    Returns:
        Optional[IntakeSession]: Stored session, or None when absent or Redis unavailable.
    """
    cid = str(client_id or "").strip()
    if not cid:
        return None

    redis = _resolve_redis(redis_client)
    if redis is None:
        return None

    key = intake_session_key(cid)
    try:
        raw = run_with_redis_retries(
            lambda: redis.get(key),
            operation_name="intake_session_get",
        )
    except Exception:
        _LOGGER.exception("failed to load intake session: client_id=%s", cid)
        return None

    return deserialize_intake_session(raw)


def save_intake_session(
    session: IntakeSession,
    *,
    redis_client: Optional[Any] = None,
    ttl_s: Optional[int] = None,
) -> bool:
    """
    Persist an intake session under the client's active key.

    Args:
        session: Session document to store.
        redis_client: Optional Redis client override.
        ttl_s: Optional TTL override in seconds.

    Returns:
        bool: True when saved successfully, False when Redis is unavailable.

    Raises:
        IntakeSessionStoreError: When Redis is configured but the write fails.
    """
    cid = str(session.client_id or "").strip()
    if not cid:
        raise ValueError("session.client_id is required")

    redis = _resolve_redis(redis_client)
    if redis is None:
        _LOGGER.warning("intake session save skipped: REDIS_URL not configured")
        return False

    key = intake_session_key(cid)
    payload = serialize_intake_session(session)
    expiry = ttl_s if ttl_s is not None else INTAKE_SESSION_TTL_S

    try:
        run_with_redis_retries(
            lambda: redis.setex(key, expiry, payload),
            operation_name="intake_session_set",
        )
        return True
    except Exception as exc:
        _LOGGER.exception("failed to save intake session: client_id=%s", cid)
        raise IntakeSessionStoreError(f"failed to save intake session for client_id={cid}") from exc


def delete_intake_session(
    client_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> bool:
    """
    Remove the active intake session for a client.

    Args:
        client_id: Client identifier.
        redis_client: Optional Redis client override.

    Returns:
        bool: True when deleted or absent, False when Redis is unavailable.
    """
    cid = str(client_id or "").strip()
    if not cid:
        return False

    redis = _resolve_redis(redis_client)
    if redis is None:
        return False

    key = intake_session_key(cid)
    try:
        run_with_redis_retries(
            lambda: redis.delete(key),
            operation_name="intake_session_delete",
        )
        return True
    except Exception:
        _LOGGER.exception("failed to delete intake session: client_id=%s", cid)
        return False


def create_intake_session(
    client_id: str,
    case_id: str,
    *,
    redis_client: Optional[Any] = None,
    replace_existing: bool = True,
) -> IntakeSession:
    """
    Create a new intake session and persist it for the client.

    Args:
        client_id: Client identifier.
        case_id: Case identifier for the new session.
        redis_client: Optional Redis client override.
        replace_existing: When False, return existing session if present.

    Returns:
        IntakeSession: Created or existing session.

    Raises:
        ValueError: When client_id or case_id is empty.
        IntakeSessionStoreError: When Redis is configured but persistence fails.
    """
    cid = str(client_id or "").strip()
    case = str(case_id or "").strip()
    if not cid or not case:
        raise ValueError("client_id and case_id are required")

    if not replace_existing:
        existing = load_intake_session(cid, redis_client=redis_client)
        if existing is not None:
            return existing

    session = new_intake_session(cid, case)
    saved = save_intake_session(session, redis_client=redis_client)
    if not saved and _resolve_redis(redis_client) is None:
        _LOGGER.warning(
            "intake session created in memory only (no redis): client_id=%s case_id=%s",
            cid,
            case,
        )
    return session


def get_or_create_intake_session(
    client_id: str,
    case_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> IntakeSession:
    """
    Load the active intake session or create one when missing.

    When the stored ``case_id`` differs from ``case_id``, the session is updated
    to the new case id (same client, new case thread).

    Args:
        client_id: Client identifier.
        case_id: Active case identifier for this turn.
        redis_client: Optional Redis client override.

    Returns:
        IntakeSession: Loaded, updated, or newly created session.
    """
    cid = str(client_id or "").strip()
    case = str(case_id or "").strip()
    if not cid or not case:
        raise ValueError("client_id and case_id are required")

    existing = load_intake_session(cid, redis_client=redis_client)
    if existing is not None:
        if existing.case_id != case:
            now_ms = int(time.time() * 1000)
            existing = existing.model_copy(
                update={
                    "case_id": case,
                    "updated_at_ms": now_ms,
                }
            )
            save_intake_session(existing, redis_client=redis_client)
        return existing

    return create_intake_session(cid, case, redis_client=redis_client)


def _load_skin_audit_for_routing(client_id: str) -> tuple[dict[str, Any], int | None]:
    """
    Load latest skin audit when skin-first routing may apply.

    Args:
        client_id: Client identifier.

    Returns:
        tuple[dict[str, Any], int | None]: Skin audit mapping and update timestamp.
    """
    if not cosmetic_skin_first_routing_active():
        return {}, None
    try:
        return load_latest_skin_audit(client_id)
    except Exception:
        _LOGGER.warning(
            "failed to load skin audit for intake recompute: client_id=%s",
            client_id,
            exc_info=True,
        )
        return {}, None


def recompute_intake_session(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    now_ms: Optional[int] = None,
) -> IntakeSession:
    """
    Recompute derived intake fields with cosmetic routing context loaded.

    Loads the latest skin audit from Redis when skin-first routing is active so
    ``missing_for_routing`` reflects snapshot freshness without persisting.

    Args:
        session: Intake session to recompute.
        profile: Current skin profile mapping.
        now_ms: Optional timestamp override for tests.

    Returns:
        IntakeSession: Session with updated phase and missing_for_routing.
    """
    skin_audit, skin_audit_updated_at_ts = _load_skin_audit_for_routing(session.client_id)
    return recompute_session(
        session,
        profile,
        now_ms=now_ms,
        cosmetic_pivot_enabled=cosmetic_pivot_enabled(),
        cosmetic_skin_first_routing=cosmetic_skin_first_routing_active(),
        cosmetic_consent_escalation_active=cosmetic_consent_escalation_active(),
        skin_audit=skin_audit,
        skin_audit_updated_at_ts=skin_audit_updated_at_ts,
    )


def save_intake_session_recomputed(
    session: IntakeSession,
    profile: Mapping[str, Any],
    *,
    redis_client: Optional[Any] = None,
    now_ms: Optional[int] = None,
) -> IntakeSession:
    """
    Recompute derived fields, refresh context summary, and persist.

    Args:
        session: Intake session after a tool mutation.
        profile: Current skin profile mapping.
        redis_client: Optional Redis client override.
        now_ms: Optional timestamp override for tests.

    Returns:
        IntakeSession: Recomputed session (also persisted when Redis is available).

    Raises:
        IntakeSessionStoreError: When Redis is configured but the write fails.
    """
    recomputed = recompute_intake_session(session, profile, now_ms=now_ms)
    summary = build_collected_context_summary(recomputed, profile)
    final = recomputed.model_copy(update={"collected_context_summary": summary})
    save_intake_session(final, redis_client=redis_client)
    return final


def archive_intake_session(
    session: IntakeSession,
    *,
    redis_client: Optional[Any] = None,
    delete_active: bool = True,
    ttl_s: Optional[int] = None,
) -> bool:
    """
    Archive a session under the case id and optionally clear the active client key.

    Args:
        session: Session to archive.
        redis_client: Optional Redis client override.
        delete_active: Whether to delete the active client-scoped key.
        ttl_s: Optional TTL override for the archive key.

    Returns:
        bool: True when archive write succeeded or Redis is unavailable (fail-open).

    Raises:
        IntakeSessionStoreError: When Redis is configured but archive write fails.
    """
    case_id = str(session.case_id or "").strip()
    if not case_id:
        raise ValueError("session.case_id is required for archive")

    redis = _resolve_redis(redis_client)
    if redis is None:
        _LOGGER.warning("intake session archive skipped: REDIS_URL not configured")
        return False

    archived = session.model_copy(
        update={
            "phase": IntakePhase.DELIVERED,
            "updated_at_ms": int(time.time() * 1000),
        }
    )
    archive_key = intake_session_archive_key(case_id)
    payload = serialize_intake_session(archived)
    expiry = ttl_s if ttl_s is not None else INTAKE_SESSION_TTL_S

    try:
        run_with_redis_retries(
            lambda: redis.setex(archive_key, expiry, payload),
            operation_name="intake_session_archive_set",
        )
    except Exception as exc:
        _LOGGER.exception("failed to archive intake session: case_id=%s", case_id)
        raise IntakeSessionStoreError(f"failed to archive intake session for case_id={case_id}") from exc

    if delete_active:
        delete_intake_session(session.client_id, redis_client=redis)
    return True


def load_archived_intake_session(
    case_id: str,
    *,
    redis_client: Optional[Any] = None,
) -> Optional[IntakeSession]:
    """
    Load an archived intake session by case id.

    Args:
        case_id: Case identifier.
        redis_client: Optional Redis client override.

    Returns:
        Optional[IntakeSession]: Archived session, or None when absent.
    """
    case = str(case_id or "").strip()
    if not case:
        return None

    redis = _resolve_redis(redis_client)
    if redis is None:
        return None

    key = intake_session_archive_key(case)
    try:
        raw = run_with_redis_retries(
            lambda: redis.get(key),
            operation_name="intake_session_archive_get",
        )
    except Exception:
        _LOGGER.exception("failed to load archived intake session: case_id=%s", case)
        return None

    return deserialize_intake_session(raw)

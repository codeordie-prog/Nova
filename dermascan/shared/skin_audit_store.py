"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
License: MIT

Redis persistence for vision-derived skin audit snapshots.

Used by skin signal flows (write) and product rules flows (read). Kept separate
from ``product_rules_signal_helper`` so unit tests avoid heavy agent imports.
"""


from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

logger = get_logger(__name__)

_SKIN_AUDIT_KEY_TEMPLATE = "skin_audit:{client_id}"
_SKIN_AUDIT_TTL_SECONDS = max(
    3600,
    int(os.getenv("SKIN_AUDIT_TTL_SECONDS", str(60 * 24 * 3600))),
)


def skin_audit_key(client_id: str) -> str:
    """
    Build Redis key for latest client skin audit snapshot.

    Args:
        client_id: Client identifier.

    Returns:
        str: Redis key string.
    """
    normalized_client_id = str(client_id or "anonymous").strip() or "anonymous"
    return _SKIN_AUDIT_KEY_TEMPLATE.format(client_id=normalized_client_id)


def _safe_json_loads(raw: str) -> dict[str, Any]:
    """
    Parse JSON object text safely.

    Args:
        raw: JSON string.

    Returns:
        dict[str, Any]: Parsed mapping or empty dict.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_signal_confidence(raw: str) -> float:
    """
    Parse MedGemma vision confidence from signal field text.

    Args:
        raw: Confidence string from Redis stream fields.

    Returns:
        float: Clamped confidence in [0.0, 1.0], or 0.0 when absent/invalid.
    """
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text)
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, value))


def load_latest_skin_audit_envelope(client_id: str) -> dict[str, Any]:
    """
    Load the full persisted skin-audit Redis envelope for a client.

    Args:
        client_id: Client identifier.

    Returns:
        dict[str, Any]: Envelope mapping or empty dict when absent.
    """
    key = skin_audit_key(client_id)
    try:
        redis_client = get_redis_client(decode_responses=True)
        raw_payload = run_with_redis_retries(
            lambda: redis_client.get(key),
            operation_name="skin_audit_get",
        )
    except Exception as exc:
        logger.warning(
            "failed to load skin audit envelope from redis: client_id=%s err=%s",
            client_id,
            exc,
        )
        return {}

    if not raw_payload:
        return {}
    parsed = _safe_json_loads(str(raw_payload))
    return parsed if isinstance(parsed, dict) else {}


def load_latest_skin_audit(client_id: str) -> tuple[dict[str, Any], int | None]:
    """
    Load latest persisted skin audit (+timestamp) from Redis for a client.

    Args:
        client_id: Client identifier.

    Returns:
        tuple[dict[str, Any], int | None]: Skin audit mapping and update timestamp.
    """
    key = skin_audit_key(client_id)
    try:
        redis_client = get_redis_client(decode_responses=True)
        raw_payload = run_with_redis_retries(
            lambda: redis_client.get(key),
            operation_name="skin_audit_get",
        )
    except Exception as exc:
        logger.warning("failed to load skin audit from redis: client_id=%s err=%s", client_id, exc)
        return {}, None

    if not raw_payload:
        return {}, None
    parsed = _safe_json_loads(str(raw_payload))
    if not isinstance(parsed, dict):
        return {}, None

    if isinstance(parsed.get("skin_audit"), dict):
        updated_at_ts_raw = parsed.get("updated_at_ts")
        try:
            updated_at_ts = int(updated_at_ts_raw) if updated_at_ts_raw is not None else None
        except Exception:
            updated_at_ts = None
        return parsed["skin_audit"], updated_at_ts

    return parsed, None


def persist_skin_audit_snapshot(
    client_id: str,
    *,
    skin_audit: Mapping[str, Any],
    request_id: str,
    primary_observation: str = "",
    diagnosis: str = "",
    risk_tier: str = "cosmetic",
    image_quality: str = "unknown",
    signal_confidence: str = "",
    snapshot_at_ts: int | None = None,
) -> bool:
    """
    Persist latest skin audit snapshot to Redis for downstream product review.

    Fail-open: logs warnings and returns False when Redis is unavailable.

    Args:
        client_id: Client identifier.
        skin_audit: Bridge skin audit mapping from rules engine report.
        request_id: Signal request id for idempotent retries.
        primary_observation: Cosmetic observation label from MedGemma.
        diagnosis: Internal clinical hypothesis from MedGemma (specialist/research use).
        risk_tier: Normalized risk tier.
        image_quality: Normalized image quality label.
        signal_confidence: MedGemma vision confidence string from signal fields.
        snapshot_at_ts: Unix timestamp for snapshot (defaults to now).

    Returns:
        bool: True when persisted successfully or already stored for request_id.
    """
    normalized_client_id = str(client_id or "anonymous").strip() or "anonymous"
    request_token = str(request_id or "").strip()
    ts = int(snapshot_at_ts if snapshot_at_ts is not None else time.time())

    audit_payload = dict(skin_audit)
    confidence = parse_signal_confidence(signal_confidence)
    if confidence > 0.0:
        audit_payload["confidence"] = confidence
    elif "confidence" not in audit_payload:
        audit_payload["confidence"] = 0.0

    if primary_observation.strip():
        audit_payload["primary_observation"] = primary_observation.strip()
    if diagnosis.strip():
        audit_payload["diagnosis"] = diagnosis.strip()
    audit_payload["risk_tier"] = str(risk_tier or audit_payload.get("risk_tier") or "cosmetic")
    audit_payload["image_quality"] = str(
        image_quality or audit_payload.get("image_quality") or "unknown"
    )
    audit_payload["snapshot_at_ts"] = ts

    envelope = {
        "schema_version": "cosmetic_v1",
        "skin_audit": audit_payload,
        "updated_at_ts": ts,
        "request_id": request_token,
    }

    key = skin_audit_key(normalized_client_id)
    try:
        redis_client = get_redis_client(decode_responses=True)
        if request_token:
            existing_raw = run_with_redis_retries(
                lambda: redis_client.get(key),
                operation_name="skin_audit_get",
            )
            if existing_raw:
                existing = _safe_json_loads(str(existing_raw))
                if (
                    isinstance(existing, dict)
                    and str(existing.get("request_id") or "").strip() == request_token
                ):
                    logger.info(
                        "skin audit snapshot already persisted: request_id=%s client_id=%s",
                        request_token,
                        normalized_client_id,
                    )
                    return True

        run_with_redis_retries(
            lambda: redis_client.set(
                key,
                json.dumps(envelope, default=str),
                ex=_SKIN_AUDIT_TTL_SECONDS,
            ),
            operation_name="skin_audit_set",
        )
        logger.info(
            "skin audit snapshot persisted: client_id=%s request_id=%s confidence=%s risk_tier=%s",
            normalized_client_id,
            request_token or "<empty>",
            audit_payload.get("confidence"),
            audit_payload.get("risk_tier"),
        )
        return True
    except Exception as exc:
        logger.warning(
            "failed to persist skin audit snapshot: client_id=%s request_id=%s err=%s",
            normalized_client_id,
            request_token or "<empty>",
            exc,
        )
        return False

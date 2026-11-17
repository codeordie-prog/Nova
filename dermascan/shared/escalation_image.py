"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-07-16
Licence: PolyForm Noncommercial License 1.0.0
Description:
Resolve durable Firebase blob paths for escalation dashboard image serving.

Escalation jobs should carry ``image_blob_path``, but some paths (rules-engine-only
escalations, URL-only references, or replay) omit it. This module normalizes URLs
to ``client-media/...`` paths and falls back to cached ingress payloads.
"""


from __future__ import annotations

import json
from typing import Any

from dermascan.shared.firebase_storage import normalize_image_blob_path
from dermascan.shared.ingress_payload import resolve_first_image_blob_path_from_payload

INGRESS_PAYLOAD_CACHE_PREFIX: str = "orchestrator:ingress_payload"
DEFAULT_IMAGE_PATH_TTL_SEC: int = 7 * 24 * 3600


def _ingress_blob_path(
    redis_client: Any,
    *,
    request_id: str,
) -> str:
    """Resolve a blob path from a cached orchestrator ingress envelope."""
    rid = str(request_id or "").strip()
    if not rid:
        return ""
    try:
        raw = redis_client.get(f"{INGRESS_PAYLOAD_CACHE_PREFIX}:{rid}")
    except Exception:
        return ""
    if not raw:
        return ""

    try:
        envelope = json.loads(str(raw))
        payload = json.loads(str(envelope.get("payload") or "{}"))
    except Exception:
        return ""

    image_blob_path = resolve_first_image_blob_path_from_payload(payload, redis_client=redis_client)
    return normalize_image_blob_path(image_blob_path)


def _signal_payload_ids(redis_client: Any, *, case_id: str) -> tuple[str, str]:
    """Read request/client ids from cached escalation signal payload, if present."""
    cid = str(case_id or "").strip()
    if not cid:
        return "", ""
    try:
        raw = redis_client.get(f"escalation:signal_payload:{cid}")
    except Exception:
        return "", ""
    if not raw:
        return "", ""
    try:
        payload = json.loads(str(raw))
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return (
        str(payload.get("request_id") or "").strip(),
        str(payload.get("client_id") or "").strip(),
    )


def resolve_escalation_image_blob_path(
    redis_client: Any,
    *,
    image_blob_path: str = "",
    request_id: str = "",
    case_id: str = "",
) -> str:
    """
    Resolve the best durable blob path for an escalated case image.

    Resolution order:
        1. Normalize ``image_blob_path`` from the escalation job.
        2. Ingress cache for ``request_id``.
        3. Ingress cache for ``case_id``.
        4. Ingress cache for ``request_id`` from ``escalation:signal_payload:{case_id}``.

    Args:
        redis_client: Redis client.
        image_blob_path: Path or URL carried on the escalation job.
        request_id: Escalation request id.
        case_id: Escalation case id.

    Returns:
        ``client-media/...`` blob path, or empty string.
    """
    direct = normalize_image_blob_path(image_blob_path)
    if direct:
        return direct

    for candidate in (request_id, case_id):
        path = _ingress_blob_path(redis_client, request_id=candidate)
        if path:
            return path

    signal_request_id, _ = _signal_payload_ids(redis_client, case_id=case_id)
    if signal_request_id and signal_request_id not in {request_id, case_id}:
        path = _ingress_blob_path(redis_client, request_id=signal_request_id)
        if path:
            return path

    return ""


def cache_escalation_image_path(
    redis_client: Any,
    *,
    case_id: str,
    blob_path: str,
    ttl_sec: int = DEFAULT_IMAGE_PATH_TTL_SEC,
) -> None:
    """
    Cache a resolved blob path for dashboard image proxy lookups.

    Args:
        redis_client: Redis client.
        case_id: Case identifier keyed on the dashboard.
        blob_path: Durable Firebase blob path.
        ttl_sec: Redis TTL in seconds.
    """
    normalized = normalize_image_blob_path(blob_path)
    cid = str(case_id or "").strip()
    if not normalized or not cid:
        return
    try:
        redis_client.setex(f"escalation:image_path:{cid}", ttl_sec, normalized)
    except Exception:
        return

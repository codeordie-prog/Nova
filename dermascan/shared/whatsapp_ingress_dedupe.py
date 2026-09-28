"""
WhatsApp ingress dedupe by platform message id (wamid).

Suppresses duplicate orchestrator jobs when Turn/Meta retries the same webhook.
"""

from __future__ import annotations

import os
from typing import Any

WHATSAPP_INGRESS_DEDUPE_TTL_SEC: int = int(
    os.getenv("WHATSAPP_INGRESS_DEDUPE_TTL_SEC", str(24 * 3600))
)


def whatsapp_ingress_seen_key(message_id: str) -> str:
    """
    Build the Redis key for one inbound wamid.

    Args:
        message_id: Inbound WhatsApp message id (wamid).

    Returns:
        Redis key used with SET NX at ingress.
    """
    return f"ingress:whatsapp:seen:{str(message_id or '').strip()}"


def claim_whatsapp_ingress_message(
    *,
    redis_client: Any,
    message_id: str,
    request_id: str,
) -> bool:
    """
    Claim first-time delivery of one inbound WhatsApp message.

    Args:
        redis_client: Redis client with ``set`` / ``delete`` support.
        message_id: Inbound wamid.
        request_id: Request id stored as the key value when claimed.

    Returns:
        True when newly claimed; False when this wamid was already accepted.
    """
    wamid = str(message_id or "").strip()
    if not wamid:
        return True
    return bool(
        redis_client.set(
            whatsapp_ingress_seen_key(wamid),
            request_id,
            nx=True,
            ex=WHATSAPP_INGRESS_DEDUPE_TTL_SEC,
        )
    )

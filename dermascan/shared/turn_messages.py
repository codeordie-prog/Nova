"""
Author: Dermascan Team
Date: 11-4-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Send WhatsApp session messages through Turn.io Messages API."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from dermascan.shared.turn_contact_api import normalize_turn_wa_id

_LOGGER: logging.Logger = logging.getLogger("dermascan.turn_messages")

_TURN_ACCEPT: str = "application/vnd.v1+json"


def _turn_api_base_url() -> str:
    """
    Return the base URL for the Turn API.

    Returns:
        str: Turn API base URL without trailing slash.
    """
    return os.getenv("TURN_API_BASE_URL", "https://whatsapp.turn.io").rstrip("/")


def _turn_api_token() -> str:
    """
    Return the Turn API bearer token.

    Returns:
        str: Token string, possibly empty when unset.
    """
    return os.getenv("TURN_API_TOKEN", "").strip()


def _turn_http_timeout_s() -> float:
    """
    Return HTTP timeout for Turn API calls.

    Returns:
        float: Timeout in seconds.
    """
    return float(os.getenv("TURN_HTTP_TIMEOUT_S", "15"))


def _turn_auth_headers() -> dict[str, str]:
    """
    Build authorization headers for Turn API requests.

    Returns:
        dict[str, str]: Headers including Bearer token.
    """
    return {
        "Authorization": f"Bearer {_turn_api_token()}",
        "Accept": _TURN_ACCEPT,
        "Content-Type": "application/json",
    }


def send_turn_whatsapp_text_sync(
    *,
    wa_id: str,
    text: str,
    reply_to_wamid: str | None = None,
) -> bool:
    """
    Send a text WhatsApp message via Turn (synchronous).

    Args:
        wa_id: Recipient WhatsApp id (digits, optional leading plus).
        text: Plain-text body.
        reply_to_wamid: Optional inbound wamid for quoted reply threading.

    Returns:
        True when Turn accepts the message (HTTP 2xx).

    Raises:
        RuntimeError: When ``TURN_API_TOKEN`` is not configured.
    """
    token = _turn_api_token()
    if not token:
        raise RuntimeError("TURN_API_TOKEN is not configured")

    to_wa = normalize_turn_wa_id(wa_id)
    body_text = str(text or "").strip()
    if not to_wa or not body_text:
        return False

    base_body: dict[str, Any] = {
        "recipient_type": "individual",
        "to": to_wa,
        "type": "text",
        "text": {"body": body_text},
    }
    url = f"{_turn_api_base_url()}/v1/messages"
    headers = _turn_auth_headers()
    reply_wamid = str(reply_to_wamid or "").strip() or None

    with httpx.Client(timeout=_turn_http_timeout_s()) as client:
        if not reply_wamid:
            response = client.post(url, headers=headers, json=base_body)
        else:
            headers_with_audit = {**headers, "X-Turn-In-Reply-To": reply_wamid}
            attempts: list[dict[str, Any]] = [
                {**base_body, "context": {"message_id": reply_wamid}},
                {**base_body, "context": {"id": reply_wamid}},
                base_body,
            ]
            response = client.post(url, headers=headers_with_audit, json=attempts[0])
            for attempt_body in attempts[1:]:
                if response.status_code < 400:
                    break
                response = client.post(url, headers=headers_with_audit, json=attempt_body)

    if response.status_code >= 400:
        _LOGGER.warning(
            "Turn text send failed: HTTP %s wa_id=%s body=%s",
            response.status_code,
            to_wa,
            response.text[:200],
        )
        return False
    return True

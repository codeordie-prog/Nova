"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-10-07
License: MIT

Synchronous Turn.io Contacts and Journeys API helpers for orchestrator tools.
"""


from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from dermascan.shared.turn_journey_routing import (
    is_turn_stack_claim_active,
    turn_main_menu_routing_enabled,
)

logger = logging.getLogger(__name__)

_TURN_ACCEPT: str = "application/vnd.v1+json"


def normalize_turn_wa_id(raw: str) -> str:
    """
    Normalize a WhatsApp id to digits without a leading plus.

    Args:
        raw: Raw wa_id from ingress or contact profile.

    Returns:
        Digits-only wa_id, or empty string when invalid.
    """
    normalized = str(raw or "").strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    return normalized


def mydawa_menu_exit_enabled() -> bool:
    """
    Return True when the MYDAWA main-menu exit tool is allowed to call Turn.

    Returns:
        bool: Controlled by ``TURN_MYDAWA_MENU_EXIT_ENABLED`` (default true).
    """
    raw = os.getenv("TURN_MYDAWA_MENU_EXIT_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _turn_api_base_url() -> str:
    """
    Return the base URL for the Turn API.
    """
    return os.getenv("TURN_API_BASE_URL", "https://whatsapp.turn.io").rstrip("/")


def _turn_api_token() -> str:
    """
    Return the API token for the Turn API.
    """
    return os.getenv("TURN_API_TOKEN", "").strip()


def _turn_main_menu_journey_uuid() -> str:
    """
    Return the UUID for the Main Menu journey.
    """
    return os.getenv("TURN_MAIN_MENU_JOURNEY_UUID", "").strip()


def _turn_http_timeout_s() -> float:    
    """
    Return the HTTP timeout for the Turn API.
    """
    return float(os.getenv("TURN_HTTP_TIMEOUT_S", "15"))


def _turn_auth_headers() -> dict[str, str]:
    """
    Return auth headers for turn.
    """
    return {
        "Authorization": f"Bearer {_turn_api_token()}",
        "Accept": _TURN_ACCEPT,
        "Content-Type": "application/json",
    }


def get_turn_contact_claim_sync(wa_id: str) -> dict[str, Any] | None:
    """
    Load the active conversation claim for a contact, if any.

    Args:
        wa_id: WhatsApp id.

    Returns:
        Claim dict when present; None when absent (HTTP 404).

    Raises:
        httpx.HTTPError: On transport or unexpected HTTP errors.
    """
    normalized = normalize_turn_wa_id(wa_id)
    if not normalized:
        return None
    url = f"{_turn_api_base_url()}/v1/contacts/{normalized}/claim"
    response = httpx.get(url, headers=_turn_auth_headers(), timeout=_turn_http_timeout_s())
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


def release_turn_contact_claim_sync(wa_id: str) -> bool:
    """
    Delete the conversation claim for a contact (release stuck Journey session).

    Args:
        wa_id: WhatsApp id.

    Returns:
        True when Turn accepts the release (HTTP 2xx).
    """
    normalized = normalize_turn_wa_id(wa_id)
    if not normalized:
        return False
    url = f"{_turn_api_base_url()}/v1/contacts/{normalized}/claim"
    response = httpx.delete(url, headers=_turn_auth_headers(), timeout=_turn_http_timeout_s())
    if response.status_code >= 400:
        logger.warning(
            "Turn claim release failed: wa_id=%s HTTP %s %s",
            normalized,
            response.status_code,
            response.text[:200],
        )
        return False
    return True


def clear_turn_active_handler_sync(wa_id: str) -> tuple[bool, str]:
    """
    Clear ``active_handler`` on the Turn contact profile.

    Args:
        wa_id: WhatsApp id.

    Returns:
        Tuple of success flag and detail message for logging or tool responses.
    """
    normalized = normalize_turn_wa_id(wa_id)
    if not normalized:
        return False, "missing wa_id"
    if not _turn_api_token():
        return False, "TURN_API_TOKEN not configured"

    url = f"{_turn_api_base_url()}/v1/contacts/{normalized}/profile"
    body = {"active_handler": None}
    response = httpx.patch(
        url,
        headers=_turn_auth_headers(),
        content=json.dumps(body),
        timeout=_turn_http_timeout_s(),
    )
    if response.status_code >= 400:
        detail = f"profile PATCH HTTP {response.status_code}: {response.text[:200]}"
        logger.warning("clear_turn_active_handler failed: wa_id=%s %s", normalized, detail)
        return False, detail
    logger.info("Turn active_handler cleared: wa_id=%s", normalized)
    return True, "ok"


def start_turn_stack_journey_sync(
    *,
    wa_id: str,
    journey_uuid: str,
    message_id: str | None = None,
) -> tuple[bool, str]:
    """
    Start a Turn Journey stack for a contact.

    Args:
        wa_id: WhatsApp id.
        journey_uuid: Stack UUID to start.
        message_id: Optional inbound wamid for journey context.

    Returns:
        Tuple of success flag and detail (error snippet when failed).
    """
    normalized = normalize_turn_wa_id(wa_id)
    journey = str(journey_uuid or "").strip()
    if not normalized or not journey:
        return False, "missing wa_id or journey_uuid"
    if not _turn_api_token():
        return False, "TURN_API_TOKEN not configured"

    payload: dict[str, str] = {"wa_id": normalized}
    wamid = str(message_id or "").strip()
    if wamid:
        payload["message_id"] = wamid

    url = f"{_turn_api_base_url()}/v1/stacks/{journey}/start"
    response = httpx.post(
        url,
        headers=_turn_auth_headers(),
        json=payload,
        timeout=_turn_http_timeout_s(),
    )
    if response.status_code == 201:
        logger.info("Turn journey started: wa_id=%s journey=%s", normalized, journey)
        return True, "ok"

    detail = f"start HTTP {response.status_code}: {response.text[:200]}"
    logger.warning("Turn journey start failed: wa_id=%s journey=%s %s", normalized, journey, detail)
    return False, detail


def return_to_mydawa_main_menu_sync(
    *,
    wa_id: str,
    message_id: str | None = None,
) -> dict[str, Any]:
    """
    Clear Dermascan routing and start the MYDAWA Main Menu Turn Journey.

    Args:
        wa_id: WhatsApp id from ingress ``client_id``.
        message_id: Inbound wamid when available.

    Returns:
        Result dict with ``status`` and ``detail`` for orchestrator tools.
    """
    if not mydawa_menu_exit_enabled():
        return {"status": "disabled", "detail": "TURN_MYDAWA_MENU_EXIT_ENABLED is off"}
    if not turn_main_menu_routing_enabled():
        return {
            "status": "misconfigured",
            "detail": "TURN_MAIN_MENU_JOURNEY_UUID or TURN_API_TOKEN missing",
        }

    journey_uuid = _turn_main_menu_journey_uuid()
    cleared, clear_detail = clear_turn_active_handler_sync(wa_id)
    if not cleared:
        return {"status": "error", "detail": f"clear active_handler failed: {clear_detail}"}

    started, start_detail = start_turn_stack_journey_sync(
        wa_id=wa_id,
        journey_uuid=journey_uuid,
        message_id=message_id,
    )
    if started:
        return {"status": "ok", "detail": "ok", "journey_uuid": journey_uuid}

    try:
        claim = get_turn_contact_claim_sync(wa_id)
    except httpx.HTTPError as exc:
        return {"status": "error", "detail": f"journey start failed: {start_detail}; claim fetch: {exc}"}

    if is_turn_stack_claim_active(claim):
        return {
            "status": "ok",
            "detail": "main menu journey already active",
            "journey_uuid": journey_uuid,
        }

    if release_turn_contact_claim_sync(wa_id):
        started_retry, retry_detail = start_turn_stack_journey_sync(
            wa_id=wa_id,
            journey_uuid=journey_uuid,
            message_id=message_id,
        )
        if started_retry:
            return {"status": "ok", "detail": "ok after claim release", "journey_uuid": journey_uuid}
        start_detail = retry_detail

    return {"status": "error", "detail": start_detail}

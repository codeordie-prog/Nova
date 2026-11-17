"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-10-07
Licence: PolyForm Noncommercial License 1.0.0

Turn.io contact routing helpers for WhatsApp ingress (Main Menu vs Dermascan).
"""


from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TURN_ACCEPT: str = "application/vnd.v1+json"
_DERMASCAN_HANDLER: str = "dermascan"


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
    Confirm everytime you shift from sandbox to production AND vice versa.
    Check the journey UUID in the Turn dashboard.
    """
    return os.getenv("TURN_MAIN_MENU_JOURNEY_UUID", "").strip()


def _turn_http_timeout_s() -> float:
    """
    Return the HTTP timeout for the Turn API.
    """
    return float(os.getenv("TURN_HTTP_TIMEOUT_S", "15"))


def turn_main_menu_routing_enabled() -> bool:
    """
    Return True when ingress should gate Nova on ``active_handler`` and start Main Menu.

    Returns:
        bool: Enabled when journey UUID and API token are configured.
    """
    return bool(_turn_main_menu_journey_uuid() and _turn_api_token())


def is_dermascan_active_handler(value: Any) -> bool:
    """
    Return True when Turn contact ``active_handler`` means Nova owns the chat.

    Args:
        value: Raw ``active_handler`` from contact profile (may be null/missing).

    Returns:
        bool: True only for ``dermascan`` (case-insensitive).
    """
    return str(value or "").strip().lower() == _DERMASCAN_HANDLER


def _turn_auth_headers() -> dict[str, str]:
    """
    Return auth headers for turn.
    """
    return {
        "Authorization": f"Bearer {_turn_api_token()}",
        "Accept": _TURN_ACCEPT,
        "Content-Type": "application/json",
    }


async def fetch_turn_active_handler(wa_id: str) -> str | None:
    """
    Read ``active_handler`` from Turn contact profile.

    Args:
        wa_id: WhatsApp id (digits, no leading plus).

    Returns:
        Field value or None when unset / missing.

    Raises:
        httpx.HTTPError: On transport failures.
    """

    # Normalize the WhatsApp id by removing the leading plus sign.
    normalized = str(wa_id or "").strip().lstrip("+")
    if not normalized:
        return None

    # Fetch the contact profile from Turn.
    url = f"{_turn_api_base_url()}/v1/contacts/{normalized}/profile"

    #Use the async client to fetch the contact profile from Turn.
    async with httpx.AsyncClient(timeout=_turn_http_timeout_s()) as client:
        response = await client.get(url, headers=_turn_auth_headers())
    if response.status_code == 404:
        return None
    
    # Raise an exception if the response is not successful.
    response.raise_for_status()
    fields = response.json().get("fields")
    if not isinstance(fields, dict):
        return None

    # Get the active handler from the contact profile.
    # Active handler is a custom field in the contact profile - Must be configured in the Turn #dashboard.
    raw = fields.get("active_handler")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def is_turn_stack_claim_active(claim: dict[str, Any] | None) -> bool:
    """
    Return True when Turn reports an active Journey (stack) claim on the contact.

    Args:
        claim: Payload from ``GET /v1/contacts/{wa_id}/claim``, or None when absent.

    Returns:
        bool: True when a non-expired ``scope=stack`` claim is present.
    """
    if not claim:
        return False
    if str(claim.get("scope") or "").strip().lower() != "stack":
        return False
    claimed_until = claim.get("claimed_until")
    if not claimed_until:
        return True
    try:
        until = datetime.fromisoformat(str(claimed_until).replace("Z", "+00:00"))
    except ValueError:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


async def fetch_turn_contact_claim(wa_id: str) -> dict[str, Any] | None:
    """
    Load the active conversation claim for a WhatsApp contact, if any.

    Args:
        wa_id: WhatsApp id (digits).

    Returns:
        Claim object when present; otherwise None.

    Raises:
        httpx.HTTPError: On transport errors (excluding 404).
    """
    # Normalize the WhatsApp id by removing the leading plus sign.
    normalized = str(wa_id or "").strip().lstrip("+")
    if not normalized:
        return None

    # Fetch the claim from Turn.
    url = f"{_turn_api_base_url()}/v1/contacts/{normalized}/claim"
    async with httpx.AsyncClient(timeout=_turn_http_timeout_s()) as client:
        response = await client.get(url, headers=_turn_auth_headers())
    if response.status_code == 404:
        return None

    # Raise an exception if the response is not successful.
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else None


async def is_turn_journey_active_for_contact(wa_id: str) -> bool:
    """
    Return True when the contact has an active Turn Journey session (stack claim).

    Args:
        wa_id: WhatsApp id.

    Returns:
        bool: True when an active stack claim exists.
    """
    try:
        claim = await fetch_turn_contact_claim(wa_id)
    except httpx.HTTPError as exc:
        logger.warning("Turn claim fetch failed for wa_id=%s: %s", wa_id, exc)
        return False
    return is_turn_stack_claim_active(claim)


async def start_turn_main_menu_journey(*, wa_id: str, message_id: str | None = None) -> bool:
    """
    Start the configured Main Menu journey for a contact.

    Args:
        wa_id: WhatsApp id (digits).
        message_id: Optional inbound wamid for journey context.

    Returns:
        True when Turn returns success.

    Raises:
        RuntimeError: When routing is not configured.
        httpx.HTTPError: On HTTP failures after logging.
    """
    if not turn_main_menu_routing_enabled():
        raise RuntimeError("TURN_MAIN_MENU_JOURNEY_UUID or TURN_API_TOKEN not configured")

    normalized_wa = str(wa_id or "").strip().lstrip("+")
    if not normalized_wa:
        logger.warning("start_turn_main_menu_journey: missing wa_id")
        return False

    body: dict[str, str] = {"wa_id": normalized_wa}
    wamid = str(message_id or "").strip()
    if wamid:
        body["message_id"] = wamid

    journey_uuid = _turn_main_menu_journey_uuid()
    url = f"{_turn_api_base_url()}/v1/stacks/{journey_uuid}/start"
    async with httpx.AsyncClient(timeout=_turn_http_timeout_s()) as client:
        response = await client.post(url, headers=_turn_auth_headers(), json=body)

    if response.status_code == 201:
        logger.info(
            "Turn Main Menu started: wa_id=%s journey=%s",
            normalized_wa,
            journey_uuid,
        )
        return True

    logger.warning(
        "Turn Main Menu start failed: wa_id=%s HTTP %s body=%s",
        normalized_wa,
        response.status_code,
        response.text[:300],
    )
    return False


async def whatsapp_ingress_should_queue_orchestrator(
    *,
    wa_id: str,
    message_id: str | None,
) -> bool:
    """
    Decide whether an inbound WhatsApp message should be enqueued for Nova.

    When Main Menu routing is enabled, only ``active_handler == dermascan`` is
    queued. All other traffic is delegated to Turn (start Main Menu once, then
    ignore while a Journey stack claim is active).

    Args:
        wa_id: Sender WhatsApp id.
        message_id: Inbound wamid.

    Returns:
        True to enqueue Nova; False to ack without orchestrator processing.
    """
    if not turn_main_menu_routing_enabled():
        return True

    try:
        handler = await fetch_turn_active_handler(wa_id)
        
    except httpx.HTTPError as exc:
        logger.warning(
            "Turn profile fetch failed for wa_id=%s: %s; delegating to Turn menu",
            wa_id,
            exc,
        )
        handler = None

    if is_dermascan_active_handler(handler):
        return True

    if await is_turn_journey_active_for_contact(wa_id):
        logger.info(
            "Turn Journey already active; skip Main Menu start and Nova: wa_id=%s",
            wa_id,
        )
        return False

    started = await start_turn_main_menu_journey(wa_id=wa_id, message_id=message_id)
    if not started and await is_turn_journey_active_for_contact(wa_id):
        logger.info(
            "Turn Main Menu start skipped (session now active): wa_id=%s",
            wa_id,
        )
    elif not started:
        logger.warning(
            "Turn Main Menu start failed; still skipping Nova for wa_id=%s",
            wa_id,
        )
    return False


async def route_whatsapp_to_main_menu_if_not_dermascan(
    *,
    wa_id: str,
    message_id: str | None,
) -> bool:
    """
    Backward-compatible helper: True when Nova should **not** process the message.

    Args:
        wa_id: Sender WhatsApp id.
        message_id: Inbound wamid.

    Returns:
        True when ingress should skip Nova enqueue (Turn menu path).
    """
    return not await whatsapp_ingress_should_queue_orchestrator(
        wa_id=wa_id,
        message_id=message_id,
    )

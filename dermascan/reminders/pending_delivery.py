"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Deliver morning briefs that were generated outside the 24h WhatsApp window."""

from __future__ import annotations

from datetime import date, datetime, timezone

from dermascan.reminders.morning_brief import (
    build_morning_brief_text,
    is_local_morning_delivery_window,
    pending_morning_catchup_expired,
)
from dermascan.reminders.outbound import enqueue_morning_brief_text_direct
from dermascan.reminders.repository import clear_morning_pending_state, get_user_state
from dermascan.reminders.window import in_service_window
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

logger = get_logger(__name__)


def try_deliver_pending_morning_brief(channel: str, client_id: str) -> bool:
    """
    Send a pending morning brief when the user opens the 24h window during local morning.

    Requires ``morning_pending_on``, an open 24h WhatsApp window, and local time inside
    the configured morning band (default 06:00–12:00 user timezone).

    Args:
        channel: Normalized or raw channel name.
        client_id: User id.

    Returns:
        bool: True when a pending brief was enqueued to outbound.

    Raises:
        None: Failures are logged; returns False.
    """
    cid = client_id.strip()
    if not cid:
        return False
    try:
        state = get_user_state(channel, cid)
    except Exception:
        logger.exception("pending morning: load state failed client_id=%s", cid)
        return False
    if not state or state.get("morning_pending_on") is None:
        return False
    now = datetime.now(timezone.utc)
    tz = str(state.get("timezone") or "Africa/Nairobi")
    ch = str(state.get("channel") or channel)

    pending_on = state.get("morning_pending_on")
    if isinstance(pending_on, datetime):
        pending_on = pending_on.date()
    if isinstance(pending_on, date) and pending_morning_catchup_expired(
        pending_on, tz, now
    ):
        try:
            clear_morning_pending_state(ch, cid)
        except Exception:
            logger.exception("failed to clear stale pending morning: client_id=%s", cid)
        else:
            logger.info(
                "cleared stale pending morning brief: client_id=%s pending_on=%s",
                cid,
                pending_on,
            )
        return False

    if not in_service_window(state.get("last_inbound_at"), now):
        return False

    if not is_local_morning_delivery_window(tz, now):
        logger.debug(
            "pending morning not enqueued (outside local morning window): client_id=%s tz=%s",
            cid,
            tz,
        )
        return False

    payload = state.get("morning_pending_payload")
    draft = ""
    if isinstance(payload, dict):
        draft = str(payload.get("draft") or "").strip()
        tz = str(payload.get("timezone") or tz)
    text = draft or build_morning_brief_text(timezone_name=tz)
    reminder_id = f"pending-{cid}"

    try:
        redis_client = get_redis_client(decode_responses=True)
        enqueue_morning_brief_text_direct(
            redis_client,
            client_id=cid,
            channel=ch,
            text=text,
            reminder_id=reminder_id,
        )
        clear_morning_pending_state(ch, cid)
    except Exception:
        logger.exception("pending morning delivery failed: client_id=%s", cid)
        return False
    logger.info("pending morning brief delivered: client_id=%s", cid)
    return True

"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Ingress-side hooks for reminder state (24h window anchor, pause on MYDAWA handoff).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dermascan.reminders.channels import channel_active_in_v1, normalize_channel
from dermascan.reminders.flags import reminders_enabled
from dermascan.reminders.pending_delivery import try_deliver_pending_morning_brief
from dermascan.reminders.repository import set_reminders_paused, touch_last_inbound
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


def _should_track(channel: str) -> bool:
    """
    Return True when reminder hooks should mutate Postgres for this channel.

    Args:
        channel: Raw ingress channel name.

    Returns:
        bool: True when ``REMINDERS_ENABLED`` and the channel is active in v1 (WhatsApp).
    """
    if not reminders_enabled():
        return False
    try:
        return channel_active_in_v1(channel)
    except ValueError:
        return False


def inbound_at_from_whatsapp_message(message: dict[str, Any]) -> datetime | None:
    """
    Parse Turn/WhatsApp webhook timestamp when present.

    Args:
        message: Single message object from the ingress payload.

    Returns:
        datetime | None: UTC-aware instant, or None to use "now" at persist time.
    """
    raw = message.get("timestamp")
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, (int, float)):
            seconds = float(raw)
        else:
            seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds > 1e12:
        seconds = seconds / 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def record_channel_inbound(
    channel: str,
    client_id: str,
    *,
    at: datetime | None = None,
) -> None:
    """
    Refresh ``last_inbound_at`` for the 24h messaging window.

    Failures are logged and swallowed so ingress never depends on Postgres.

    Args:
        channel: Ingress channel (v1: WhatsApp only when enabled).
        client_id: Stable user id on that channel.
        at: Optional platform timestamp; defaults to now in the repository.

    Returns:
        None: Always; failures are logged and not re-raised.
    """
    if not _should_track(channel):
        return
    cid = client_id.strip()
    if not cid:
        return
    try:
        ch = normalize_channel(channel)
        touch_last_inbound(ch, cid, at=at)
        try_deliver_pending_morning_brief(ch, cid)
    except Exception:
        logger.exception(
            "reminders touch_last_inbound failed: channel=%s client_id=%s",
            channel,
            cid,
        )


def resume_reminders_after_nova_ingress(channel: str, client_id: str) -> None:
    """
    Clear ``reminders_paused`` when Nova owns the conversation again.

    Args:
        channel: Ingress channel.
        client_id: User id.

    Returns:
        None: Always; failures are logged and not re-raised.
    """
    if not _should_track(channel):
        return
    cid = client_id.strip()
    if not cid:
        return
    try:
        ch = normalize_channel(channel)
        set_reminders_paused(ch, cid, paused=False)
        try_deliver_pending_morning_brief(ch, cid)
    except Exception:
        logger.exception(
            "reminders resume after ingress failed: channel=%s client_id=%s",
            channel,
            cid,
        )


def pause_reminders_after_platform_handoff(channel: str, client_id: str) -> None:
    """
    Pause proactive reminders when the user leaves Nova for MYDAWA / Turn menu.

    Args:
        channel: Ingress channel.
        client_id: User id (WhatsApp wa_id in v1).

    Returns:
        None: Always; failures are logged and not re-raised.
    """
    if not _should_track(channel):
        return
    cid = client_id.strip()
    if not cid:
        return
    try:
        ch = normalize_channel(channel)
        set_reminders_paused(ch, cid, paused=True)
    except Exception:
        logger.exception(
            "reminders pause after handoff failed: channel=%s client_id=%s",
            channel,
            cid,
        )

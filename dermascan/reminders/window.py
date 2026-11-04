"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

WhatsApp customer service window policy (24h after last user inbound).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

SERVICE_WINDOW_HOURS: Final[int] = 24


def service_window_end(last_inbound_at: datetime | None) -> datetime | None:
    """
    Compute when the free-form messaging window closes.

    Args:
        last_inbound_at: Timestamp of the user's last inbound message, UTC-aware.

    Returns:
        datetime | None: Window end in UTC, or None when there is no anchor.
    """
    if last_inbound_at is None:
        return None
    anchor = last_inbound_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor + timedelta(hours=SERVICE_WINDOW_HOURS)


def in_service_window(
    last_inbound_at: datetime | None,
    now: datetime,
) -> bool:
    """
    Return True when proactive free-form sends are allowed.

    Args:
        last_inbound_at: User's last inbound timestamp (UTC-aware preferred).
        now: Current time (UTC-aware preferred).

    Returns:
        bool: True when ``now`` is strictly before window end.
    """
    end = service_window_end(last_inbound_at)
    if end is None:
        return False
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current < end

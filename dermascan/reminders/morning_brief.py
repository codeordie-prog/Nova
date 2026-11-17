"""Author: Dermascan Team
Date: 11-1-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Morning briefing copy (v1 template; replace with weather/UV enrichment later)."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Final
from zoneinfo import ZoneInfo

_DEFAULT_START: Final[str] = "06:00"
_DEFAULT_END: Final[str] = "12:00"


def _parse_local_hhmm(raw: str, *, field_name: str) -> time:
    """
    Parse a local wall-clock time from ``HH:MM``.

    Args:
        raw: Time string from configuration.
        field_name: Name for error messages.

    Returns:
        time: Parsed local time.

    Raises:
        ValueError: When the format is invalid.
    """
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be HH:MM, got {raw!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field_name} out of range: {raw!r}")
    return time(hour=hour, minute=minute)


def morning_delivery_window_local() -> tuple[time, time]:
    """
    Load the inclusive start and exclusive end of the pending morning delivery window.

    Configured via ``REMINDERS_MORNING_DELIVERY_START_LOCAL`` and
    ``REMINDERS_MORNING_DELIVERY_END_LOCAL`` (defaults ``06:00``–``12:00`` local).

    Returns:
        tuple[time, time]: ``(start_inclusive, end_exclusive)`` in local wall time.
    """
    start_raw = os.getenv("REMINDERS_MORNING_DELIVERY_START_LOCAL", _DEFAULT_START)
    end_raw = os.getenv("REMINDERS_MORNING_DELIVERY_END_LOCAL", _DEFAULT_END)
    return (
        _parse_local_hhmm(start_raw, field_name="REMINDERS_MORNING_DELIVERY_START_LOCAL"),
        _parse_local_hhmm(end_raw, field_name="REMINDERS_MORNING_DELIVERY_END_LOCAL"),
    )


def is_local_morning_delivery_window(timezone_name: str, now: datetime) -> bool:
    """
    Return True when ``now`` falls in the configured morning band in the user's timezone.

    Used for pending morning briefs so a missed 07:00 fire is not sent at night when
    the user messages again—only during the morning section (default 06:00–12:00 local).

    Args:
        timezone_name: IANA timezone for the user.
        now: Reference instant (UTC-aware preferred).

    Returns:
        bool: True when local time satisfies ``start <= t < end``.
    """
    instant = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = timezone.utc
    local_time = instant.astimezone(tz).time()
    start_local, end_local = morning_delivery_window_local()
    if start_local <= end_local:
        return start_local <= local_time < end_local
    # Overnight window (unusual): e.g. 22:00–06:00
    return local_time >= start_local or local_time < end_local


def local_calendar_date(timezone_name: str, now: datetime) -> date:
    """
    User's local calendar date for ``now``.

    Args:
        timezone_name: IANA timezone.
        now: Reference instant (UTC-aware preferred).

    Returns:
        date: Local date (UTC date if timezone is invalid).
    """
    instant = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    try:
        return instant.astimezone(ZoneInfo(timezone_name)).date()
    except Exception:
        return instant.date()


def pending_morning_catchup_expired(
    pending_on: date,
    timezone_name: str,
    now: datetime,
    *,
    grace_days: int = 1,
) -> bool:
    """
    Return True when a pending morning brief is too old to deliver.

    Catch-up is allowed on ``pending_on`` and the next ``grace_days`` local calendar
    days (default: same day + one following day), only inside the morning band.

    Args:
        pending_on: Date the brief was generated but not sent.
        timezone_name: User IANA timezone.
        now: Current instant.
        grace_days: Extra local days after ``pending_on`` still eligible.

    Returns:
        bool: True when pending should be cleared without sending.
    """
    if grace_days < 0:
        grace_days = 0
    local_today = local_calendar_date(timezone_name, now)
    last_valid = pending_on + timedelta(days=grace_days)
    return local_today > last_valid


def build_morning_brief_text(*, timezone_name: str) -> str:
    """
    Build the user-facing morning brief message for v1.

    Args:
        timezone_name: IANA timezone stored for the user (for future localized content).

    Returns:
        str: Short proactive WhatsApp message body.
    """
    _ = timezone_name
    return (
        "Good morning! ☀️ Quick skincare check-in: use SPF if you are heading out, "
        "stay hydrated, and patch-test anything new on your routine. "
        "Message me anytime if you want today's plan tailored to you."
        "Remember, Beauty requires careful regard, for without a sense of it, existence loses part of its meaning."
    )

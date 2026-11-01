"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Pure schedule parsing and next-fire computation (timezone-aware).
"""

from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
from typing import Any, Final, Literal, TypedDict, cast
from zoneinfo import ZoneInfo

from dermascan.reminders.kinds import MORNING_BRIEF_LOCAL_TIME

ScheduleType = Literal["daily_time", "interval_hours"]

DAILY_TIME: Final[ScheduleType] = "daily_time"
INTERVAL_HOURS: Final[ScheduleType] = "interval_hours"


class DailyTimeSchedule(TypedDict):
    """Fire once per local calendar day at ``local_time`` (HH:MM)."""

    type: Literal["daily_time"]
    local_time: str


class IntervalHoursSchedule(TypedDict):
    """Fire every ``every_hours`` from anchor or from ``after`` on first run."""

    type: Literal["interval_hours"]
    every_hours: int


ScheduleSpec = DailyTimeSchedule | IntervalHoursSchedule


def morning_brief_schedule() -> DailyTimeSchedule:
    """
    Default schedule document for the morning briefing kind.

    Returns:
        DailyTimeSchedule: Fixed 07:00 local fire spec.
    """
    return {"type": DAILY_TIME, "local_time": MORNING_BRIEF_LOCAL_TIME}


def parse_schedule(raw: dict[str, Any]) -> ScheduleSpec:
    """
    Validate a schedule JSON object from Postgres.

    Args:
        raw: ``schedule`` column value.

    Returns:
        ScheduleSpec: Typed schedule.

    Raises:
        ValueError: When shape or values are invalid.
    """
    if not isinstance(raw, dict):
        raise ValueError("schedule must be a JSON object")
    kind = raw.get("type")
    if kind == DAILY_TIME:
        local_time = str(raw.get("local_time", "")).strip()
        _parse_local_time(local_time)
        return cast(DailyTimeSchedule, {"type": DAILY_TIME, "local_time": local_time})
    if kind == INTERVAL_HOURS:
        every = raw.get("every_hours")
        if not isinstance(every, int) or every < 1 or every > 168:
            raise ValueError("every_hours must be an integer from 1 to 168")
        return cast(IntervalHoursSchedule, {"type": INTERVAL_HOURS, "every_hours": every})
    raise ValueError(f"unsupported schedule type {kind!r}")


def _parse_local_time(local_time: str) -> time:
    """
    Parse HH:MM local time.

    Args:
        local_time: Time string.

    Returns:
        time: Parsed time.

    Raises:
        ValueError: On bad format.
    """
    parts = local_time.split(":")
    if len(parts) != 2:
        raise ValueError(f"local_time must be HH:MM, got {local_time!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"local_time must be HH:MM, got {local_time!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"local_time out of range: {local_time!r}")
    return time(hour=hour, minute=minute)


def _ensure_utc(dt: datetime) -> datetime:
    """
    Normalize a datetime to UTC for schedule comparisons.

    Args:
        dt: Aware or naive datetime.

    Returns:
        datetime: UTC-aware instant.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_next_fire_at(
    schedule_raw: dict[str, Any],
    *,
    timezone_name: str,
    after: datetime,
    last_fired_at: datetime | None = None,
) -> datetime:
    """
    Compute the next UTC fire instant strictly after ``after``.

    Args:
        schedule_raw: JSON schedule from a reminder row.
        timezone_name: IANA timezone for daily_time schedules.
        after: Reference instant (typically now UTC).
        last_fired_at: Previous fire time for interval schedules.

    Returns:
        datetime: Next fire time in UTC.

    Raises:
        ValueError: When schedule or timezone is invalid.
    """
    spec = parse_schedule(schedule_raw)
    reference = _ensure_utc(after)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"invalid timezone {timezone_name!r}") from exc

    if spec["type"] == INTERVAL_HOURS:
        every = spec["every_hours"]
        anchor = last_fired_at if last_fired_at is not None else reference
        anchor_utc = _ensure_utc(anchor)
        candidate = anchor_utc + timedelta(hours=every)
        if candidate <= reference:
            # Advance until strictly after reference.
            delta = reference - anchor_utc
            steps = int(delta.total_seconds() // (every * 3600)) + 1
            candidate = anchor_utc + timedelta(hours=every * steps)
        return candidate

    local_time = _parse_local_time(spec["local_time"])
    local_after = reference.astimezone(tz)
    candidate_local = datetime.combine(local_after.date(), local_time, tzinfo=tz)
    if candidate_local <= local_after:
        candidate_local = datetime.combine(
            local_after.date() + timedelta(days=1),
            local_time,
            tzinfo=tz,
        )
    return candidate_local.astimezone(timezone.utc)

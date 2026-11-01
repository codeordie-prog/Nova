"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for reminder schedule parsing and next-fire computation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dermascan.reminders.scheduling import (
    compute_next_fire_at,
    morning_brief_schedule,
    parse_schedule,
)


def test_morning_brief_schedule_shape() -> None:
    spec = morning_brief_schedule()
    assert spec["type"] == "daily_time"
    assert spec["local_time"] == "07:00"


def test_parse_interval_schedule() -> None:
    spec = parse_schedule({"type": "interval_hours", "every_hours": 4})
    assert spec["every_hours"] == 4


def test_parse_rejects_bad_interval() -> None:
    with pytest.raises(ValueError):
        parse_schedule({"type": "interval_hours", "every_hours": 0})


def test_daily_time_next_fire_next_local_day() -> None:
    # 2026-06-01 05:00 UTC = 08:00 Africa/Nairobi; next 07:00 local is 2026-06-02 04:00 UTC
    after = datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)
    nxt = compute_next_fire_at(
        {"type": "daily_time", "local_time": "07:00"},
        timezone_name="Africa/Nairobi",
        after=after,
    )
    assert nxt == datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)


def test_interval_next_fire_from_now() -> None:
    after = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    nxt = compute_next_fire_at(
        {"type": "interval_hours", "every_hours": 3},
        timezone_name="Africa/Nairobi",
        after=after,
    )
    assert nxt == datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)

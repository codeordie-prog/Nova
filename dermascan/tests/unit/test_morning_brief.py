"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for morning brief copy."""

from __future__ import annotations

from datetime import date, datetime, timezone

from dermascan.reminders.morning_brief import (
    build_morning_brief_text,
    is_local_morning_delivery_window,
    pending_morning_catchup_expired,
)


def test_build_morning_brief_non_empty() -> None:
    text = build_morning_brief_text(timezone_name="Africa/Nairobi")
    assert "morning" in text.lower()
    assert len(text) > 40


def test_morning_window_nairobi_9am_local() -> None:
    # 2026-06-01 06:00 UTC = 09:00 Africa/Nairobi (UTC+3)
    now = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
    assert is_local_morning_delivery_window("Africa/Nairobi", now) is True


def test_morning_window_nairobi_8pm_local() -> None:
    # 2026-06-01 17:00 UTC = 20:00 Africa/Nairobi
    now = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
    assert is_local_morning_delivery_window("Africa/Nairobi", now) is False


def test_pending_catchup_expires_after_grace_day() -> None:
    pending = date(2026, 6, 1)
    # 2026-06-03 local Nairobi — past pending + 1 grace day
    now = datetime(2026, 6, 3, 7, 0, tzinfo=timezone.utc)
    assert pending_morning_catchup_expired(pending, "Africa/Nairobi", now) is True


def test_pending_catchup_valid_on_next_day() -> None:
    pending = date(2026, 6, 1)
    now = datetime(2026, 6, 2, 7, 0, tzinfo=timezone.utc)
    assert pending_morning_catchup_expired(pending, "Africa/Nairobi", now) is False

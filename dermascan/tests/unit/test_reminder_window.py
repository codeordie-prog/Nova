"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for the 24h service window policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dermascan.reminders.window import in_service_window, service_window_end


def test_service_window_end_adds_24_hours() -> None:
    start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    end = service_window_end(start)
    assert end == start + timedelta(hours=24)


def test_in_service_window_inside() -> None:
    start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    now = start + timedelta(hours=23, minutes=59)
    assert in_service_window(start, now) is True


def test_in_service_window_outside() -> None:
    start = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    now = start + timedelta(hours=24)
    assert in_service_window(start, now) is False


def test_in_service_window_no_anchor() -> None:
    now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert in_service_window(None, now) is False

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for due-reminder processing (window and pause gates)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from dermascan.reminders import processor


def _base_row(**overrides: object) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(),
        "channel": "whatsapp",
        "client_id": "254700000001",
        "kind": "routine_reminder",
        "routine_key": "sunscreen",
        "label": "Sunscreen",
        "schedule": {"type": "interval_hours", "every_hours": 4},
        "timezone": "Africa/Nairobi",
        "reminders_paused": False,
        "last_inbound_at": now - timedelta(hours=1),
    }
    row.update(overrides)
    return row


def test_process_skips_when_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    apply = MagicMock()
    monkeypatch.setattr(processor, "apply_fire_outcome", apply)
    redis = MagicMock()
    outcome = processor.process_due_reminder_row(redis, _base_row(reminders_paused=True))
    assert outcome == "skipped_paused"
    redis.assert_not_called()
    apply.assert_called_once()


def test_process_routine_enqueues_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueue = MagicMock()
    apply = MagicMock()
    monkeypatch.setattr(processor, "enqueue_routine_reminder_text", enqueue)
    monkeypatch.setattr(processor, "apply_fire_outcome", apply)
    outcome = processor.process_due_reminder_row(MagicMock(), _base_row())
    assert outcome == "sent"
    enqueue.assert_called_once()
    apply.assert_called_once()


def test_process_morning_outside_window_sets_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    apply = MagicMock()
    monkeypatch.setattr(processor, "apply_fire_outcome", apply)
    old_inbound = datetime.now(timezone.utc) - timedelta(hours=30)
    row = _base_row(kind="morning_briefing", schedule={"type": "daily_time", "local_time": "07:00"}, last_inbound_at=old_inbound)
    outcome = processor.process_due_reminder_row(MagicMock(), row)
    assert outcome == "skipped_outside_window"
    kwargs = apply.call_args.kwargs
    assert kwargs.get("morning_pending_on") is not None

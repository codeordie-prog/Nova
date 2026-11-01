"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for pending morning brief delivery gates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from dermascan.reminders import pending_delivery


def test_pending_clears_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 5, 7, 0, tzinfo=timezone.utc)
    clear = MagicMock()
    monkeypatch.setattr(
        pending_delivery,
        "get_user_state",
        lambda _ch, _cid: {
            "channel": "whatsapp",
            "timezone": "Africa/Nairobi",
            "morning_pending_on": date(2026, 6, 1),
            "last_inbound_at": now - timedelta(minutes=1),
        },
    )
    monkeypatch.setattr(pending_delivery, "clear_morning_pending_state", clear)
    assert pending_delivery.try_deliver_pending_morning_brief("whatsapp", "254700000001") is False
    clear.assert_called_once()


def test_pending_skips_outside_local_morning(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        pending_delivery,
        "get_user_state",
        lambda _ch, _cid: {
            "channel": "whatsapp",
            "timezone": "Africa/Nairobi",
            "morning_pending_on": datetime(2026, 6, 1).date(),
            "morning_pending_payload": {"draft": "Good morning"},
            "last_inbound_at": now - timedelta(minutes=5),
        },
    )
    monkeypatch.setattr(pending_delivery, "is_local_morning_delivery_window", lambda _tz, _now: False)
    enqueue = MagicMock()
    monkeypatch.setattr(pending_delivery, "enqueue_morning_brief_text_direct", enqueue)
    assert pending_delivery.try_deliver_pending_morning_brief("whatsapp", "254700000001") is False
    enqueue.assert_not_called()

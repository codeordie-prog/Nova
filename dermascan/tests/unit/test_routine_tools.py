"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for orchestrator routine reminder tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dermascan.backend.services.tools.orchestrator import routine_tools


class TestRoutineTools:
    """Tools return status dicts and respect REMINDERS_ENABLED."""

    def test_set_morning_brief_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "false")
        out = routine_tools.set_morning_brief.invoke(
            {
                "client_id": "254700000001",
                "request_id": "req-1",
                "enabled": True,
            }
        )
        assert out["status"] == "reminders_disabled"

    def test_set_routine_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        monkeypatch.setattr(
            routine_tools,
            "_ingress_envelope",
            lambda _rid: {"channel": "whatsapp", "client_id": "254700000001"},
        )
        upsert = MagicMock(
            return_value={
                "id": "00000000-0000-0000-0000-000000000001",
                "kind": "routine_reminder",
                "routine_key": "sunscreen",
                "enabled": True,
                "schedule": {"type": "interval_hours", "every_hours": 4},
            }
        )
        monkeypatch.setattr(routine_tools, "upsert_routine_reminder", upsert)
        out = routine_tools.set_routine_reminder.invoke(
            {
                "client_id": "254700000001",
                "request_id": "req-1",
                "label": "Sunscreen",
                "every_hours": 4,
            }
        )
        assert out["status"] == "ok"
        assert out["routine_key"] == "sunscreen"
        upsert.assert_called_once()

    def test_set_routine_rejects_both_schedules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        monkeypatch.setattr(
            routine_tools,
            "_ingress_envelope",
            lambda _rid: {"channel": "whatsapp", "client_id": "254700000001"},
        )
        out = routine_tools.set_routine_reminder.invoke(
            {
                "client_id": "254700000001",
                "request_id": "req-1",
                "label": "Retinol",
                "every_hours": 4,
                "daily_local_time": "21:00",
            }
        )
        assert out["status"] == "invalid_schedule"

    def test_list_routines_telegram_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        monkeypatch.setattr(
            routine_tools,
            "_ingress_envelope",
            lambda _rid: {"channel": "telegram", "client_id": "10001"},
        )
        out = routine_tools.list_routines.invoke(
            {"client_id": "10001", "request_id": "req-1"}
        )
        assert out["status"] == "unsupported_channel"

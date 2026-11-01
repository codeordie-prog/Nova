"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for reminder ingress hooks (gated by REMINDERS_ENABLED)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from dermascan.reminders import ingress_hooks


class TestReminderIngressHooks:
    """Hooks must no-op when disabled and must not raise on repository errors."""

    def test_record_skips_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "false")
        touch = MagicMock()
        monkeypatch.setattr(ingress_hooks, "touch_last_inbound", touch)
        ingress_hooks.record_channel_inbound("whatsapp", "254700000001")
        touch.assert_not_called()

    def test_record_whatsapp_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        touch = MagicMock()
        monkeypatch.setattr(ingress_hooks, "touch_last_inbound", touch)
        at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        ingress_hooks.record_channel_inbound("whatsapp", "254700000001", at=at)
        touch.assert_called_once_with("whatsapp", "254700000001", at=at)

    def test_record_skips_telegram_in_v1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        touch = MagicMock()
        monkeypatch.setattr(ingress_hooks, "touch_last_inbound", touch)
        ingress_hooks.record_channel_inbound("telegram", "10001")
        touch.assert_not_called()

    def test_pause_on_handoff_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        pause = MagicMock()
        monkeypatch.setattr(ingress_hooks, "set_reminders_paused", pause)
        ingress_hooks.pause_reminders_after_platform_handoff("whatsapp", "254700000001")
        pause.assert_called_once_with("whatsapp", "254700000001", paused=True)

    def test_resume_after_nova_ingress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")
        resume = MagicMock()
        monkeypatch.setattr(ingress_hooks, "set_reminders_paused", resume)
        ingress_hooks.resume_reminders_after_nova_ingress("whatsapp", "254700000001")
        resume.assert_called_once_with("whatsapp", "254700000001", paused=False)

    def test_inbound_at_from_whatsapp_seconds(self) -> None:
        at = ingress_hooks.inbound_at_from_whatsapp_message({"timestamp": "1713000000"})
        assert at == datetime.fromtimestamp(1713000000, tz=timezone.utc)

    def test_repository_error_does_not_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REMINDERS_ENABLED", "true")

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(ingress_hooks, "touch_last_inbound", _boom)
        ingress_hooks.record_channel_inbound("whatsapp", "254700000001")

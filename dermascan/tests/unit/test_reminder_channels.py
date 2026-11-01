"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for reminder channel normalization and v1 gating."""

from __future__ import annotations

import pytest

from dermascan.reminders.channels import channel_active_in_v1, normalize_channel


def test_normalize_whatsapp() -> None:
    assert normalize_channel(" WhatsApp ") == "whatsapp"


def test_normalize_telegram_allowed_in_schema() -> None:
    assert normalize_channel("telegram") == "telegram"


def test_v1_active_whatsapp_only() -> None:
    assert channel_active_in_v1("whatsapp") is True
    assert channel_active_in_v1("telegram") is False


def test_normalize_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        normalize_channel("sms")

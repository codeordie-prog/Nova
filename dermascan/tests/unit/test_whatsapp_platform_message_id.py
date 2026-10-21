"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Tests for WhatsApp platform message id helpers."""


from __future__ import annotations

from dermascan.shared.whatsapp_platform_message_id import (
    is_whatsapp_platform_message_id,
    resolve_channel_reply_target,
)


def test_accepts_wamid_prefix() -> None:
    assert is_whatsapp_platform_message_id("wamid.HBgL123") is True


def test_rejects_internal_request_uuid_hex() -> None:
    assert (
        is_whatsapp_platform_message_id("a1b2c3d4e5f6789012345678901234ab") is False
    )


def test_rejects_telegram_numeric_id() -> None:
    assert is_whatsapp_platform_message_id("99") is False


def test_resolve_whatsapp_reply_target() -> None:
    wamid = "wamid.HBgL123"
    assert resolve_channel_reply_target(channel="whatsapp", message_id=wamid) == wamid


def test_resolve_skips_request_id_on_whatsapp() -> None:
    assert (
        resolve_channel_reply_target(
            channel="whatsapp",
            message_id="a1b2c3d4e5f6789012345678901234ab",
        )
        is None
    )

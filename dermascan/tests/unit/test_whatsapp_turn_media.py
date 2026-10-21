"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Tests for Turn WhatsApp media URL detection."""


from __future__ import annotations

from dermascan.shared.whatsapp_turn_media import is_whatsapp_protected_media_url


def test_protected_lookaside_url() -> None:
    """Meta lookaside hosts require authenticated download."""
    url = (
        "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1&hash=abc"
    )
    assert is_whatsapp_protected_media_url(url) is True


def test_public_https_url_not_protected() -> None:
    """Ordinary HTTPS image URLs are not treated as protected."""
    assert is_whatsapp_protected_media_url("https://cdn.example.com/photo.jpg") is False


def test_empty_url_not_protected() -> None:
    """Empty values are not protected URLs."""
    assert is_whatsapp_protected_media_url("") is False

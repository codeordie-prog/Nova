"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Tests for WhatsApp ingress wamid dedupe."""


from __future__ import annotations

from dermascan.shared.whatsapp_ingress_dedupe import whatsapp_ingress_seen_key


def test_whatsapp_ingress_seen_key_uses_wamid() -> None:
    wamid = "wamid.HBgL123"
    assert whatsapp_ingress_seen_key(wamid) == f"ingress:whatsapp:seen:{wamid}"

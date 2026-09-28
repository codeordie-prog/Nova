"""
Helpers for WhatsApp platform message ids (wamid / Turn message ids).

Distinguishes inbound WhatsApp ids from internal request ids and Telegram numeric ids.
"""

from __future__ import annotations


def is_whatsapp_platform_message_id(value: str) -> bool:
    """
    Return True when ``value`` is a plausible WhatsApp/Turn message id for quoting.

    Args:
        value: Candidate from ingress ``message.id`` or outbound ``reply_to_message_id``.

    Returns:
        bool: True for wamid-shaped or Turn connector ids; False for request uuids, etc.
    """
    candidate = str(value or "").strip()
    if not candidate:
        return False
    if candidate.startswith("wamid."):
        return True
    if candidate.isdigit():
        return False
    lowered = candidate.lower()
    if len(candidate) == 32 and all(ch in "0123456789abcdef" for ch in lowered):
        return False
    return len(candidate) >= 12


def resolve_channel_reply_target(*, channel: str, message_id: str) -> str | None:
    """
    Pick an outbound ``reply_to_message_id`` safe for the delivery channel.

    Args:
        channel: Ingress channel (``whatsapp``, ``telegram``, …).
        message_id: Source message id from the job or signal stream.

    Returns:
        Reply target string, or None when not quotable on that channel.
    """
    mid = str(message_id or "").strip()
    if not mid:
        return None
    ch = str(channel or "").strip().lower()
    if ch == "whatsapp":
        return mid if is_whatsapp_platform_message_id(mid) else None
    if ch == "telegram" and mid.isdigit():
        return mid
    return None

"""
Author: Dermascan Team
Date: 11-4-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

WhatsApp ingress media policy: allow photos and voice; reject documents and video."""

from __future__ import annotations

from typing import Any, Final

_ACCEPTED_TYPES: Final[frozenset[str]] = frozenset({"text", "image", "audio", "voice"})

_MSG_VIDEO: Final[str] = (
    "Videos aren't supported for skin or product review. "
    "Please send a clear photo (use the camera or gallery Photo option, not a file attachment)."
)

_MSG_DOCUMENT: Final[str] = (
    "Documents and file attachments aren't supported. "
    "Please send your skin or product label as a photo — tap attach and choose Photo, not Document."
)

_MSG_UNSUPPORTED: Final[str] = (
    "That message type isn't supported here. "
    "Send a text message, a voice note, or a photo to continue."
)


def rejection_message_for_whatsapp_message(message: dict[str, Any]) -> str | None:
    """
    Return user-facing rejection copy when ingress must not reach the orchestrator.

    Args:
        message: Raw WhatsApp message object from a Turn/Meta webhook.

    Returns:
        Instruction text to send via Turn when rejected; None when allowed.
    """
    msg_type = str(message.get("type") or "text").strip().lower()

    attachment_rejection = _rejection_for_attachments(message)
    if attachment_rejection is not None:
        return attachment_rejection

    if msg_type in _ACCEPTED_TYPES:
        return None
    if msg_type == "video":
        return _MSG_VIDEO
    if msg_type == "document":
        return _MSG_DOCUMENT
    return _MSG_UNSUPPORTED


def _rejection_for_attachments(message: dict[str, Any]) -> str | None:
    """
    Reject legacy ``attachments[]`` payloads that are not images.

    Args:
        message: Raw WhatsApp message object.

    Returns:
        Rejection copy or None when attachments are absent or image-only.
    """
    raw_attachments = message.get("attachments") or []
    if isinstance(raw_attachments, dict):
        raw_attachments = [raw_attachments]
    if not isinstance(raw_attachments, list):
        return None

    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, dict):
            continue
        attachment_type = str(raw_attachment.get("type") or "image").strip().lower()
        if attachment_type != "image":
            if attachment_type == "video":
                return _MSG_VIDEO
            if attachment_type == "document":
                return _MSG_DOCUMENT
            return _MSG_UNSUPPORTED
    return None

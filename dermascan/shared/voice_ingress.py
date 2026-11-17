"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Ingress payload helpers for WhatsApp voice notes (Turn ``type: voice``).
"""

from __future__ import annotations

import json
from typing import Any, Final, Optional

VOICE_NOTE_EXTRA_KEY: Final[str] = "inbound_voice_note"
VOICE_TRANSCRIPT_PREFIX: Final[str] = "(Voice note transcript)"


def payload_inbound_voice_note(payload: dict[str, Any]) -> bool:
    """
    Return True when the ingress payload marks an inbound WhatsApp voice note.

    Args:
        payload: Job ingress JSON (content + optional metadata).

    Returns:
        bool: True for Turn PTT ``voice`` / ``audio`` messages with an audio attachment.
    """
    media_id, _mime = first_whatsapp_audio_media_id(payload)
    extras = (payload.get("metadata") or {}).get("extras") or {}
    if not isinstance(extras, dict):
        extras = {}

    if str(extras.get(VOICE_NOTE_EXTRA_KEY) or "").lower() in {"true", "1", "yes"}:
        return True

    whatsapp_type = str(extras.get("whatsapp_type") or "").lower()
    if whatsapp_type == "voice":
        return True
    if whatsapp_type == "audio" and media_id:
        return True

    # Backstop: audio-only ingress without metadata (older jobs or partial payloads).
    if media_id:
        content = payload.get("content") or {}
        text = str(content.get("text") or "").strip() if isinstance(content, dict) else ""
        if not text:
            return True
    return False


def first_whatsapp_audio_media_id(payload: dict[str, Any]) -> tuple[str, str]:
    """
    Resolve Turn media id and MIME type from the first audio attachment.

    Args:
        payload: Job ingress JSON.

    Returns:
        Tuple of (media_id, mime_type). Empty media_id when none found.
    """
    attachments = (payload.get("content") or {}).get("attachments") or []
    if not isinstance(attachments, list):
        return "", "audio/ogg"
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "audio":
            continue
        source = item.get("source") or {}
        if not isinstance(source, dict):
            continue
        if str(source.get("kind") or "") != "whatsapp_media_id":
            continue
        media_id = str(source.get("value") or "").strip()
        mime_type = str(item.get("mime_type") or "audio/ogg").strip()
        if media_id:
            return media_id, mime_type
    return "", "audio/ogg"


def merge_transcript_into_payload(payload: dict[str, Any], transcript: str) -> dict[str, Any]:
    """
    Inject STT transcript into ingress payload text for the orchestrator.

    Args:
        payload: Original ingress payload dict.
        transcript: Speech-to-text result.

    Returns:
        dict[str, Any]: Shallow copy with updated ``content.text``.
    """
    merged = json.loads(json.dumps(payload, default=str))
    content = merged.setdefault("content", {})
    if not isinstance(content, dict):
        content = {}
        merged["content"] = content
    existing = str(content.get("text") or "").strip()
    body = str(transcript or "").strip()
    if body:
        line = f"{VOICE_TRANSCRIPT_PREFIX}\n{body}"
        content["text"] = f"{existing}\n\n{line}".strip() if existing else line
    return merged


def encode_payload_json(payload: dict[str, Any]) -> str:
    """
    Serialize ingress payload for Redis job fields.

    Args:
        payload: Ingress dict.

    Returns:
        str: JSON string.
    """
    return json.dumps(payload, default=str)

"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for WhatsApp voice-note ingress helpers.
"""

from dermascan.shared.voice_ingress import (
    VOICE_TRANSCRIPT_PREFIX,
    first_whatsapp_audio_media_id,
    merge_transcript_into_payload,
    payload_inbound_voice_note,
)


def test_payload_inbound_voice_note_from_extras() -> None:
    payload = {"metadata": {"extras": {"inbound_voice_note": "true"}}}
    assert payload_inbound_voice_note(payload) is True


def test_payload_inbound_voice_note_from_whatsapp_type() -> None:
    payload = {"metadata": {"extras": {"whatsapp_type": "voice"}}}
    assert payload_inbound_voice_note(payload) is True


def test_payload_inbound_voice_note_audio_attachment_without_metadata() -> None:
    payload = {
        "content": {
            "attachments": [
                {
                    "type": "audio",
                    "mime_type": "audio/ogg",
                    "source": {"kind": "whatsapp_media_id", "value": "mid-99"},
                }
            ]
        }
    }
    assert payload_inbound_voice_note(payload) is True


def test_first_whatsapp_audio_media_id() -> None:
    payload = {
        "content": {
            "attachments": [
                {
                    "type": "audio",
                    "mime_type": "audio/ogg",
                    "source": {"kind": "whatsapp_media_id", "value": "media-123"},
                }
            ]
        }
    }
    media_id, mime = first_whatsapp_audio_media_id(payload)
    assert media_id == "media-123"
    assert mime == "audio/ogg"


def test_merge_transcript_into_payload() -> None:
    payload = {"content": {"text": "caption"}}
    merged = merge_transcript_into_payload(payload, "hello world")
    text = str((merged.get("content") or {}).get("text") or "")
    assert VOICE_TRANSCRIPT_PREFIX in text
    assert "hello world" in text
    assert "caption" in text

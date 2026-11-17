"""Unit tests for WhatsApp ingress media rejection policy."""

from __future__ import annotations

from dermascan.shared.whatsapp_ingress_media_policy import rejection_message_for_whatsapp_message


def test_accepts_image_and_voice_types() -> None:
    """Photos and voice notes may reach the orchestrator."""
    assert rejection_message_for_whatsapp_message({"type": "image", "from": "1", "id": "m1"}) is None
    assert rejection_message_for_whatsapp_message({"type": "voice", "from": "1", "id": "m2"}) is None
    assert rejection_message_for_whatsapp_message({"type": "audio", "from": "1", "id": "m3"}) is None
    assert rejection_message_for_whatsapp_message({"type": "text", "text": {"body": "hi"}}) is None


def test_rejects_video_and_document() -> None:
    """Video and documents get instructional copy."""
    video_msg = rejection_message_for_whatsapp_message(
        {"type": "video", "from": "1", "id": "m4", "video": {"mime_type": "video/mp4"}}
    )
    assert video_msg is not None
    assert "Video" in video_msg or "video" in video_msg.lower()

    doc_msg = rejection_message_for_whatsapp_message(
        {
            "type": "document",
            "from": "1",
            "id": "m5",
            "document": {"mime_type": "application/pdf", "filename": "report.pdf"},
        }
    )
    assert doc_msg is not None
    assert "Document" in doc_msg or "photo" in doc_msg.lower()


def test_rejects_image_sent_as_document_type() -> None:
    """Image MIME under document type still counts as a document attachment."""
    msg = rejection_message_for_whatsapp_message(
        {
            "type": "document",
            "from": "1",
            "id": "m6",
            "document": {"mime_type": "image/jpeg", "filename": "skin.jpg"},
        }
    )
    assert msg is not None
    assert "Photo" in msg or "photo" in msg


def test_accepts_interactive_journey_handoff() -> None:
    """Menu button taps from Turn Journey must not trigger media rejection."""
    payload = {
        "type": "interactive",
        "from": "254700000000",
        "id": "wamid.interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": "skin_snapshot", "title": "Skin snapshot"},
        },
    }
    assert rejection_message_for_whatsapp_message(payload) is None


def test_rejects_non_image_attachments_array() -> None:
    """Legacy attachments[] with document type is rejected."""
    msg = rejection_message_for_whatsapp_message(
        {
            "type": "text",
            "from": "1",
            "id": "m7",
            "attachments": [{"type": "document", "mime_type": "application/pdf"}],
        }
    )
    assert msg is not None

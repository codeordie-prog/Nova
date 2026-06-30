"""Valid and invalid ingress envelopes for webhook and orchestrator endpoints."""

from __future__ import annotations

import time
from typing import Any


def whatsapp_text_webhook(
    *,
    from_id: str = "254700000001",
    message_id: str = "wamid.p95-probe",
    text: str = "P95 ingress probe",
) -> dict[str, Any]:
    """Valid WhatsApp Cloud API text message envelope for /v1/webhooks/whatsapp."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": from_id,
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def whatsapp_image_webhook(
    *,
    from_id: str = "254700000001",
    message_id: str = "wamid.e2e-image",
    media_ref: str,
    text: str = "Please diagnose this rash",
    attachment_id: str = "img-e2e-1",
) -> dict[str, Any]:
    """WhatsApp webhook with a blob_ref attachment for /v1/webhooks/whatsapp."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "message": {
                                "from": from_id,
                                "id": message_id,
                                "type": "text",
                                "text": {"body": text},
                                "attachments": [
                                    {
                                        "id": attachment_id,
                                        "type": "image",
                                        "media_ref": media_ref,
                                        "mime_type": "image/jpeg",
                                        "caption": text,
                                    }
                                ],
                            }
                        }
                    }
                ]
            }
        ]
    }


def whatsapp_valid_envelope() -> dict[str, Any]:
    """Default valid WhatsApp webhook payload."""
    return whatsapp_text_webhook()


def whatsapp_invalid_envelope_missing_fields() -> dict[str, Any]:
    """Webhook payload missing required fields (expect ignored or 4xx)."""
    return {"entry": [{"changes": [{"value": {"messages": [{}]}}]}]}


def telegram_text_webhook(
    *,
    chat_id: int = 10001,
    user_id: int = 10001,
    message_id: int = 99,
    text: str = "P95 ingress probe",
    update_id: int | None = None,
) -> dict[str, Any]:
    """Valid Telegram Bot API update envelope for /v1/webhooks/telegram."""
    return {
        "update_id": update_id if update_id is not None else int(time.time() * 1000),
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "is_bot": False, "username": "p95_probe"},
            "chat": {"id": chat_id, "type": "private"},
            "date": int(time.time()),
            "text": text,
        },
    }


def telegram_valid_envelope() -> dict[str, Any]:
    """Default valid Telegram webhook payload."""
    return telegram_text_webhook()


def orchestrator_request_valid() -> dict[str, Any]:
    """Minimal valid orchestrator request payload scaffold."""
    return {
        "client_id": "client-scaffold-1",
        "request_type": "diagnose",
        "payload": {"prompt": "Test skin condition"},
    }


def orchestrator_batch_valid() -> dict[str, Any]:
    """Minimal valid orchestrator batch request payload scaffold."""
    return {
        "requests": [orchestrator_request_valid()],
    }

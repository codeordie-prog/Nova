"""
Author: Dermascan Team
Date: 2026-09-24
License: MIT

Backend API endpoint drafts for orchestrator request intake.

These are contract-first placeholders and intentionally contain no implementation.
"""

import base64
import binascii
from decimal import Decimal, InvalidOperation
import asyncio
import hmac
import json
import os
import uuid
from typing import Any, Dict

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

from fastapi import APIRouter, Body, File, Header, HTTPException, Request, Response, UploadFile
import redis
import requests

from dermascan.backend.models.requests import (
    AttachmentItem,
    AttachmentSource,
    MessageContent,
    OrchestratorIngressRequest,
    RequestMetadata,
    RoutingHint,
)
from psycopg.types.json import Jsonb

from dermascan.billing import admin_queries as billing_admin
from dermascan.billing.db import transaction as billing_transaction
from dermascan.billing.flags import billing_enabled, paystack_is_live
from dermascan.billing.webhook import (
    SIGNATURE_HEADER as PAYSTACK_SIGNATURE_HEADER,
    dedupe_key as paystack_dedupe_key,
    is_paystack_ip,
    verify_signature as verify_paystack_signature,
)
from dermascan.backend.api.group_media_helper import (
    append_to_media_batch,
    claim_media_batch,
    enrich_payload_with_group_metadata,
    wait_for_media_batch_quiet,
)
from dermascan.shared.escalation_image import (
    DEFAULT_IMAGE_PATH_TTL_SEC,
    cache_escalation_image_path,
    resolve_escalation_image_blob_path,
)
from dermascan.shared.firebase_storage import upload_client_image, download_client_image_bytes_by_path
from dermascan.shared.turn_journey_routing import whatsapp_ingress_should_queue_orchestrator
from dermascan.shared.turn_webhook_auth import verify_whatsapp_ingress_auth
from dermascan.shared.whatsapp_ingress_dedupe import (
    claim_whatsapp_ingress_message,
    whatsapp_ingress_seen_key,
)
from dermascan.shared.whatsapp_turn_media import (
    fetch_turn_media_bytes,
    is_whatsapp_protected_media_url,
)
from dermascan.shared.global_rate_limiter import _enforce_global_rate_limit
from dermascan.shared.redis_stability import get_redis_client
from dermascan.shared.hmac_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify as verify_hmac
from dermascan.shared.logger import get_logger
from dermascan.shared.queue_depth_manager import _wait_for_queue_capacity
from dermascan.shared.stream_pipeline_tracing import (
    enrich_stream_fields,
    record_stream_produce,
)

router = APIRouter(prefix="/v1", tags=["orchestrator"])


logger = get_logger(__name__)

_REDIS_URL: str = os.getenv("REDIS_URL", "")
_ORCHESTRATOR_JOB_STREAM: str = os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
_WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
_MEDIA_TTL_S: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_MEDIA_BLOB_TTL_S: int = int(os.getenv("MEDIA_BLOB_TTL_S", str(DEFAULT_IMAGE_PATH_TTL_SEC)))
_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
_TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_HTTP_TIMEOUT_S: float = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "30"))
_TELEGRAM_TLS_VERIFY: str = os.getenv("TELEGRAM_TLS_VERIFY", "true")
_TELEGRAM_CA_BUNDLE: str = os.getenv("TELEGRAM_CA_BUNDLE", "")
_TELEGRAM_MEDIA_GROUP_WINDOW_S: float = float(os.getenv("TELEGRAM_MEDIA_GROUP_WINDOW_S", "1.2"))
_TELEGRAM_MEDIA_GROUP_MAX_WAIT_S: float = float(os.getenv("TELEGRAM_MEDIA_GROUP_MAX_WAIT_S", "4.0"))
_WHATSAPP_INGRESS_SECRET: str = os.getenv(
    "WHATSAPP_INGRESS_SECRET",
    os.getenv("INTERNAL_API_SECRET", ""),
)
_TURN_WEBHOOK_SECRET: str = os.getenv("TURN_WEBHOOK_SECRET", "")
_INGRESS_RETRY_AFTER_S: str = os.getenv("INGRESS_RETRY_AFTER_SECONDS", "1")
_ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
_EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
_ESCALATION_CLOSED_TTL_SEC: int = 7 * 24 * 3600
_BILLING_JOB_STREAM: str = os.getenv("BILLING_JOB_STREAM", "billing:jobs")

# Shared pooled clients (same caps as workers; avoids extra from_url pools).
_redis = get_redis_client(decode_responses=True) if _REDIS_URL else None
_redis_binary = get_redis_client(decode_responses=False) if _REDIS_URL else None


def _validate_image_bytes(image_bytes: bytes) -> None:
    """
    Validates the image bytes.
    
    Args:
        image_bytes (bytes): The image bytes to validate.
        
    Raises:
        HTTPException: If the image bytes are invalid.
    """
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload")

    # Validate size limit
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"Image too large (max {_MAX_IMAGE_BYTES} bytes)")

    # Check format
    head: bytes = image_bytes[:16]
    is_jpeg: bool = head.startswith(b"\xff\xd8\xff")
    is_png: bool = head.startswith(b"\x89PNG\r\n\x1a\n")
    is_gif: bool = head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    is_webp: bool = len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"

    if not (is_jpeg or is_png or is_gif or is_webp):
        raise HTTPException(status_code=400, detail="Unsupported or invalid image format")


def _store_uploaded_client_image(
    *,
    channel: str,
    client_id: str,
    message_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> tuple[AttachmentSource, str, int]:
    """
    Upload a client image and cache its durable blob path in Redis.

    Args:
        channel: Ingress channel label (``telegram`` or ``whatsapp``).
        client_id: End-user identifier.
        message_id: Source message id for logging.
        image_bytes: Validated image bytes.
        mime_type: Image MIME type.

    Returns:
        Tuple of attachment source, media ref key, and byte length.

    Raises:
        HTTPException: When Firebase upload or Redis cache fails.
    """
    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    try:
        uploaded = upload_client_image(
            client_id=client_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
    except Exception as exc:
        logger.exception(
            "Firebase client image upload failed: client_id=%s message_id=%s",
            client_id,
            message_id,
        )
        raise HTTPException(status_code=502, detail="Failed to store image") from exc

    media_ref = f"media:{channel}:{client_id}:{uuid.uuid4().hex}"
    try:
        _redis.setex(media_ref, _MEDIA_BLOB_TTL_S, uploaded.blob_path)
    except redis.RedisError as exc:
        logger.exception(
            "Redis media blob cache failed: media_ref=%s client_id=%s",
            media_ref,
            client_id,
        )
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

    return AttachmentSource(kind="blob_ref", value=media_ref), media_ref, len(image_bytes)


def _store_whatsapp_image_attachment(
    *,
    client_id: str,
    message_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> tuple[AttachmentSource, int]:
    """Upload WhatsApp image bytes to Firebase and return a blob_ref attachment source."""
    source, _, size_bytes = _store_uploaded_client_image(
        channel="whatsapp",
        client_id=client_id,
        message_id=message_id,
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    return source, size_bytes


def _resolve_whatsapp_image_attachment_source(
    *,
    client_id: str,
    message_id: str,
    media_id: str,
    media_ref: str,
    image_url: str,
    image_b64: str,
    mime_type: str,
) -> tuple[AttachmentSource, int | None]:
    """
    Resolve a WhatsApp image to a durable attachment source for orchestrator ingress.

    Turn/Meta webhooks often include ``lookaside.fbsbx.com`` URLs that require bearer
    auth; when ``media_id`` is present those are fetched via Turn and stored in Firebase.

    Args:
        client_id: Sender wa_id.
        message_id: Inbound wamid.
        media_id: Cloud API media id from ``image.id``.
        media_ref: Pre-resolved sidecar blob ref when present.
        image_url: Optional CDN URL from the webhook.
        image_b64: Optional inline base64 from a sidecar.
        mime_type: Declared MIME type.

    Returns:
        Attachment source and optional size in bytes.

    Raises:
        HTTPException: On invalid bytes, fetch failure, or missing resolvable media.
    """
    if image_b64:
        try:
            image_bytes: bytes = base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid image base64") from exc
        _validate_image_bytes(image_bytes)
        source, size_bytes = _store_whatsapp_image_attachment(
            client_id=client_id,
            message_id=message_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        return source, size_bytes

    if media_ref:
        return AttachmentSource(kind="blob_ref", value=media_ref), None

    protected_url = is_whatsapp_protected_media_url(image_url)
    should_fetch = bool(media_id) and (protected_url or not image_url)
    if should_fetch:
        try:
            fetched_bytes, fetched_mime = fetch_turn_media_bytes(media_id)
        except (RuntimeError, ValueError) as exc:
            logger.exception(
                "Turn media fetch failed: media_id=%s message_id=%s",
                media_id,
                message_id,
            )
            raise HTTPException(status_code=502, detail=f"Failed to fetch WhatsApp media: {exc}") from exc
        resolved_mime = fetched_mime if fetched_mime.startswith("image/") else mime_type
        _validate_image_bytes(fetched_bytes)
        source, size_bytes = _store_whatsapp_image_attachment(
            client_id=client_id,
            message_id=message_id,
            image_bytes=fetched_bytes,
            mime_type=resolved_mime,
        )
        return source, size_bytes

    if image_url.lower().startswith(("http://", "https://")) and not protected_url:
        return AttachmentSource(kind="url", value=image_url), None

    if protected_url and not media_id:
        raise HTTPException(
            status_code=400,
            detail="WhatsApp image URL requires auth; missing image.id for Turn media fetch",
        )

    if media_id:
        return AttachmentSource(kind="whatsapp_media_id", value=media_id), None

    return AttachmentSource(kind="whatsapp_media_id", value="unknown"), None


async def _enforce_ingress_capacity(*, cost: int = 1) -> None:
    """
    Apply global RPM and orchestrator queue-depth gates before enqueueing.

    Args:
        cost: Ingress cost for this webhook (e.g. number of messages).

    Raises:
        HTTPException: If rate limit or queue pressure blocks intake.
    """
    await _enforce_global_rate_limit(cost=cost)
    state, _ = await _wait_for_queue_capacity()
    if state == "red":
        raise HTTPException(
            status_code=429,
            detail="Service is overloaded. Please try again later.",
            headers={"Retry-After": _INGRESS_RETRY_AFTER_S},
        )


# Extract whatsapp messages - This logic may change when we get the META API
def _extract_whatsapp_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract all WhatsApp message objects from the webhook payload.

    Args:
        payload: Raw webhook payload.

    Returns:
        A list of message objects in source order.
    """
    out: list[dict[str, Any]] = []

    # Turn.io inbound webhooks use Meta flat shape: messages[] at top level
    # (not wrapped in entry[].changes[].value). See Turn webhook docs.
    top_level_messages = payload.get("messages")
    if isinstance(top_level_messages, list):
        for raw_message in top_level_messages:
            if isinstance(raw_message, dict) and raw_message:
                out.append(raw_message)
        if out:
            return out

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Backward compatibility for legacy/custom shape.
            message_obj = value.get("message")
            if isinstance(message_obj, dict) and message_obj:
                out.append(message_obj)

            # Current Cloud API shape.
            legacy_messages = value.get("messages") or []
            if isinstance(legacy_messages, list):
                for raw_message in legacy_messages:
                    if isinstance(raw_message, dict) and raw_message:
                        out.append(raw_message)
    return out


# Extract telegram message
def _extract_telegram_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract Telegram message from the payload.
    
    Args:
        payload (dict[str, Any]): The payload to extract the message from.
        
    Returns:
        dict[str, Any] | None: The extracted message or None if not found.
    """
    message_obj = payload.get("message")
    if isinstance(message_obj, dict) and message_obj:
        return message_obj

    edited_message_obj = payload.get("edited_message")
    if isinstance(edited_message_obj, dict) and edited_message_obj:
        return edited_message_obj

    return None

# Get MIME type from file path
def _telegram_mime_from_file_path(file_path: str) -> str:
    """
    Get the MIME type from the file path.
    
    Args:
        file_path (str): The file path to get the MIME type from.
        
    Returns:
        str: The MIME type.
    """
    lowered = file_path.lower()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".gif"):
        return "image/gif"
    if lowered.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """
    Download a file from Telegram.
    
    This function downloads a file from Telegram using the provided file ID.
    It uses the TELEGRAM_BOT_TOKEN environment variable to authenticate the request.
    It also uses the TELEGRAM_CA_BUNDLE and TELEGRAM_TLS_VERIFY environment variables
    to configure the TLS verification.

    Args:
        file_id (str): The file ID to download.
        
    Returns:
        tuple[bytes, str]: The file content and MIME type.
    """
    if not _TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not configured")

    verify: bool | str = True
    if _TELEGRAM_CA_BUNDLE.strip():
        verify = _TELEGRAM_CA_BUNDLE.strip()
    elif _TELEGRAM_TLS_VERIFY.strip().lower() in {"0", "false", "no", "off"}:
        verify = False

    get_file_url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/getFile"

    try:
        response = requests.get(
            get_file_url,
            params={"file_id": file_id},
            timeout=_TELEGRAM_HTTP_TIMEOUT_S,
            verify=verify,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Telegram getFile failed: file_id=%s", file_id)
        raise HTTPException(status_code=502, detail="Telegram getFile request failed") from exc

    if not payload.get("ok"):
        raise HTTPException(status_code=502, detail="Telegram getFile returned non-ok response")

    result = payload.get("result") or {}
    file_path = str(result.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=502, detail="Telegram getFile missing file_path")

    file_url = f"https://api.telegram.org/file/bot{_TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        response = requests.get(
            file_url,
            timeout=_TELEGRAM_HTTP_TIMEOUT_S,
            verify=verify,
        )
        response.raise_for_status()
        file_bytes = response.content
    except requests.RequestException as exc:
        logger.exception("Telegram file download failed: file_path=%s", file_path)
        raise HTTPException(status_code=502, detail="Telegram file download failed") from exc

    return file_bytes, _telegram_mime_from_file_path(file_path)


def _normalize_message(message: dict[str, Any]) -> OrchestratorIngressRequest:
    """
    Normalize a message from the payload.
    
    This function normalizes a message from the payload by extracting the client ID,
    message ID, message type, text parts, and attachments.
    
    Args:
        message (dict[str, Any]): The message to normalize.
        
    Returns:
        OrchestratorIngressRequest: The normalized message.
    """
    client_id: str = str(message.get("from") or "").strip()
    message_id: str = str(message.get("id") or "").strip()
    if not client_id or not message_id:
        raise HTTPException(status_code=400, detail="Invalid WhatsApp payload: missing from/id")

    msg_type: str = str(message.get("type") or "text")
    text_parts: list[str] = []
    attachments: list[AttachmentItem] = []

    raw_text = message.get("text")
    if isinstance(raw_text, dict):
        text_body = str(raw_text.get("body") or "").strip()
    else:
        text_body = str(raw_text or "").strip()
    if text_body:
        text_parts.append(text_body)

    raw_attachments = message.get("attachments") or []
    if isinstance(raw_attachments, dict):
        raw_attachments = [raw_attachments]

    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, dict):
            continue

        attachment_type = str(raw_attachment.get("type") or "image")
        mime_type = str(
            raw_attachment.get("mime_type")
            or ("image/*" if attachment_type == "image" else "application/octet-stream")
        )
        caption = str(raw_attachment.get("caption") or "").strip()
        if caption:
            text_parts.append(caption)

        attachment_id = str(raw_attachment.get("id") or "")
        media_ref = str(raw_attachment.get("media_ref") or raw_attachment.get("blob_ref") or "")
        attachment_url = str(raw_attachment.get("url") or "").strip()
        media_id = str(raw_attachment.get("media_id") or "")
        image_b64 = str(raw_attachment.get("base64") or raw_attachment.get("data_base64") or raw_attachment.get("image_base64") or "")

        if attachment_type == "image":
            source, resolved_size = _resolve_whatsapp_image_attachment_source(
                client_id=client_id,
                message_id=message_id,
                media_id=media_id or attachment_id,
                media_ref=media_ref,
                image_url=attachment_url,
                image_b64=image_b64,
                mime_type=mime_type,
            )
            size_bytes = resolved_size if resolved_size is not None else raw_attachment.get("size_bytes")
        elif image_b64:
            source = AttachmentSource(kind="base64", value=image_b64)
            size_bytes = raw_attachment.get("size_bytes")
        elif media_ref:
            source = AttachmentSource(kind="blob_ref", value=media_ref)
            size_bytes = raw_attachment.get("size_bytes")
        elif attachment_url.lower().startswith(("http://", "https://")):
            source = AttachmentSource(kind="url", value=attachment_url)
            size_bytes = raw_attachment.get("size_bytes")
        else:
            source = AttachmentSource(kind="whatsapp_media_id", value=media_id or "unknown")
            size_bytes = raw_attachment.get("size_bytes")

        attachments.append(
            AttachmentItem(
                id=attachment_id or f"att-{uuid.uuid4().hex}",
                type="image" if attachment_type == "image" else "document",
                mime_type=mime_type,
                filename=raw_attachment.get("filename"),
                size_bytes=size_bytes,
                source=source,
            )
        )

    if msg_type == "text":
        pass

    elif msg_type == "image":
        image_obj: dict[str, Any] = message.get("image") or {}
        caption: str = str(image_obj.get("caption") or "")
        if caption:
            text_parts.append(caption)

        media_id: str = str(image_obj.get("id") or "")
        media_ref: str = str(image_obj.get("media_ref") or "")
        image_url: str = str(image_obj.get("url") or "").strip()
        mime_type: str = str(image_obj.get("mime_type") or "image/*")
        image_b64: str = str(image_obj.get("base64") or image_obj.get("data_base64") or "")

        source, resolved_size = _resolve_whatsapp_image_attachment_source(
            client_id=client_id,
            message_id=message_id,
            media_id=media_id,
            media_ref=media_ref,
            image_url=image_url,
            image_b64=image_b64,
            mime_type=mime_type,
        )
        size_bytes = resolved_size if resolved_size is not None else image_obj.get("file_size")

        attachments.append(
            AttachmentItem(
                id=media_id or f"img-{uuid.uuid4().hex}",
                type="image",
                mime_type=mime_type,
                filename=image_obj.get("filename"),
                size_bytes=size_bytes,
                source=source,
            )
        )

    elif msg_type == "document":
        document_obj: dict[str, Any] = message.get("document") or {}
        caption: str = str(document_obj.get("caption") or "")
        if caption:
            text_parts.append(caption)

        media_id: str = str(document_obj.get("id") or "")
        media_ref: str = str(document_obj.get("media_ref") or "")
        document_url: str = str(document_obj.get("url") or "").strip()
        doc_b64: str = str(document_obj.get("base64") or document_obj.get("data_base64") or "")
        mime_type: str = str(document_obj.get("mime_type") or "application/octet-stream")
        source: AttachmentSource

        if doc_b64:
            source = AttachmentSource(kind="base64", value=doc_b64)
        elif media_ref:
            source = AttachmentSource(kind="blob_ref", value=media_ref)
        elif document_url.lower().startswith(("http://", "https://")):
            source = AttachmentSource(kind="url", value=document_url)
        else:
            source = AttachmentSource(kind="whatsapp_media_id", value=media_id or "unknown")

        attachments.append(
            AttachmentItem(
                id=media_id or f"doc-{uuid.uuid4().hex}",
                type="document",
                mime_type=mime_type,
                filename=document_obj.get("filename"),
                size_bytes=document_obj.get("file_size"),
                source=source,
            )
        )

    content = MessageContent(text="\n".join([part for part in text_parts if part]).strip() or None, attachments=attachments)
    if not content.text and not content.attachments:
        raise HTTPException(status_code=400, detail=f"Unsupported or empty message type: {msg_type}")

    _rid = uuid.uuid4().hex
    return OrchestratorIngressRequest(
        request_id=_rid,
        case_id=_rid,  # each escalation is its own case; client_id (phone) is delivery only
        client_id=client_id,
        message_id=message_id,
        conversation_id=client_id,
        channel="whatsapp",
        content=content,
        routing=RoutingHint(ingress_stream=_ORCHESTRATOR_JOB_STREAM, priority="normal"),
        metadata=RequestMetadata(extras={"whatsapp_type": msg_type}),
    )


def _normalize_telegram_message(message: Dict[str, Any]) -> OrchestratorIngressRequest:
    """
    Normalize a message from the Telegram payload.
    
    This function normalizes a message from the Telegram payload by extracting the client ID,
    message ID, message type, text parts, and attachments.
    
    Args:
        message (dict[str, Any]): The message to normalize.
        
    Returns:
        OrchestratorIngressRequest: The normalized message.
    """
    chat_obj = message.get("chat") or {}
    from_obj = message.get("from") or {}

    conversation_id = str(chat_obj.get("id") or "").strip()
    client_id = str(from_obj.get("id") or conversation_id).strip()
    message_id = str(message.get("message_id") or "").strip()
    username = str(from_obj.get("username") or "").strip()

    if not conversation_id or not client_id or not message_id:
        raise HTTPException(status_code=400, detail="Invalid Telegram payload: missing chat_id/from_id/message_id")

    text_parts: list[str] = []
    attachments: list[AttachmentItem] = []

    # Get user prompt
    text = str(message.get("text") or "").strip()
    if text:
        text_parts.append(text)

    # Get caption
    caption = str(message.get("caption") or "").strip()
    if caption:
        text_parts.append(caption)

    metadata_extras: dict[str, str] = {
        "telegram_chat_type": str(chat_obj.get("type") or ""),
        "telegram_username": str(from_obj.get("username") or ""),
    }

    # Get photos
    photos: list = message.get("photo") or []
    if photos:
        if _redis is None:
            raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

        # Get largest photo - Telegram sends multiple sizes, we want the largest for best quality
        largest_photo = None
        largest_size = -1
        for photo in photos:
            if not isinstance(photo, dict):
                continue
            this_size = int(photo.get("file_size") or 0)
            if this_size > largest_size:
                largest_size = this_size
                largest_photo = photo

        # Download the largest photo
        if isinstance(largest_photo, dict):
            file_id = str(largest_photo.get("file_id") or "").strip()
            if file_id:
                image_bytes, mime_type = _download_telegram_file(file_id)
                _validate_image_bytes(image_bytes)

                source, media_ref, _ = _store_uploaded_client_image(
                    channel="telegram",
                    client_id=client_id,
                    message_id=message_id,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                )

                metadata_extras["telegram_media_ref"] = media_ref

                attachments.append(
                    AttachmentItem(
                        id=str(largest_photo.get("file_unique_id") or f"tg-{uuid.uuid4().hex}"),
                        type="image",
                        mime_type=mime_type,
                        filename=None,
                        size_bytes=len(image_bytes),
                        source=source,
                    )
                )

    content = MessageContent(text="\n".join([part for part in text_parts if part]).strip() or None, attachments=attachments)
    if not content.text and not content.attachments:
        raise HTTPException(status_code=400, detail="Unsupported or empty Telegram message")

    _rid = uuid.uuid4().hex
    return OrchestratorIngressRequest(
        request_id=_rid,
        case_id=_rid,  # each escalation is its own case; client_id (phone) is delivery only
        client_id=client_id,
        message_id=message_id,
        conversation_id=conversation_id,
        username=username,
        channel="telegram",
        content=content,
        routing=RoutingHint(ingress_stream=_ORCHESTRATOR_JOB_STREAM, priority="normal"),
        metadata=RequestMetadata(extras=metadata_extras),
    )


def _payload_content_json(normalized: OrchestratorIngressRequest) -> str:
    """
    Convert the content of an OrchestratorIngressRequest to a JSON string.
    
    Args:
        normalized: The normalized OrchestratorIngressRequest
        
    Returns:
        A JSON string representation of the content
    """
    # Get content
    content = normalized.content

    # Extract attachments from content
    attachments = [
        att.model_dump() if hasattr(att, "model_dump") else att.dict()
        for att in content.attachments
    ]

    payload: dict[str, Any] = {
        "content": {
            "text": content.text,
            "attachments": attachments,
        }
    }
    if normalized.metadata.extras:
        payload["metadata"] = {"extras": normalized.metadata.extras}

    return json.dumps(payload, default=str)


def _enqueue_orchestrator_row(
    *,
    normalized: OrchestratorIngressRequest,
    payload_override: str | None = None,
) -> None:
    """
    Enqueue a normalized ingress message to orchestrator stream.

    Args:
        normalized: Normalized ingress request.
        payload_override: Optional payload JSON override.
    """
    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")
    payload_json = payload_override if payload_override is not None else _payload_content_json(normalized)
    stream_fields = enrich_stream_fields(
        {
            "request_id": normalized.request_id,
            "case_id": normalized.case_id,
            "client_id": normalized.client_id,
            "message_id": normalized.message_id,
            "channel": normalized.channel,
            "username": normalized.username or "",
            "payload": payload_json,
        }
    )
    msg_id = str(
        _redis.xadd(
            _ORCHESTRATOR_JOB_STREAM,
            stream_fields,
            maxlen=_WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    )
    record_stream_produce(
        stream=_ORCHESTRATOR_JOB_STREAM,
        msg_id=msg_id,
        fields=stream_fields,
        extra_metadata={"worker": "ingress"},
    )


async def _flush_telegram_media_group_after_window(*, channel: str, group_id: str) -> None:
    """
    Wait for album quiet window then enqueue grouped Telegram items.

    Args:
        channel: Channel name.
        group_id: Telegram media group id.
    """
    if _redis is None:
        return
    became_quiet = await wait_for_media_batch_quiet(
        _redis,
        channel=channel,
        group_key=group_id,
        quiet_window_s=_TELEGRAM_MEDIA_GROUP_WINDOW_S,
        max_wait_s=_TELEGRAM_MEDIA_GROUP_MAX_WAIT_S,
    )
    if not became_quiet:
        return

    slots = claim_media_batch(_redis, channel=channel, group_key=group_id)
    if not slots:
        return
    expected_count = len(slots)

    for slot in slots:
        payload_json = str(slot.get("payload_json") or "")
        if not payload_json:
            continue
        enriched_payload = enrich_payload_with_group_metadata(
            payload_json=payload_json,
            group_id=group_id,
            group_expected_count=expected_count,
        )
        row = slot.get("row") if isinstance(slot.get("row"), dict) else {}
        if not row:
            continue
        try:
            stream_fields = enrich_stream_fields(
                {
                    "request_id": str(row.get("request_id") or ""),
                    "case_id": str(row.get("case_id") or ""),
                    "client_id": str(row.get("client_id") or ""),
                    "message_id": str(row.get("message_id") or ""),
                    "channel": str(row.get("channel") or "telegram"),
                    "username": str(row.get("username") or ""),
                    "payload": enriched_payload,
                }
            )
            msg_id = str(
                _redis.xadd(
                    _ORCHESTRATOR_JOB_STREAM,
                    stream_fields,
                    maxlen=_WORKERS_STREAM_MAXLEN,
                    approximate=True,
                )
            )
            record_stream_produce(
                stream=_ORCHESTRATOR_JOB_STREAM,
                msg_id=msg_id,
                fields=stream_fields,
                extra_metadata={"worker": "ingress", "flow": "telegram_media_group"},
            )
        except redis.RedisError:
            logger.exception("failed enqueue for telegram media group member: group_id=%s", group_id)


@router.post("/orchestrator/requests")
async def enqueue_orchestrator_request(request: Request) -> None:
    pass


@router.post("/orchestrator/requests/batch")
async def enqueue_orchestrator_requests_batch(request: Request) -> None:
    pass


@router.get("/orchestrator/requests/{request_id}")
async def get_orchestrator_request_status(request_id: str) -> None:
    pass


@router.post("/media/test/upload")
async def upload_test_media(image: UploadFile = File(...)) -> dict[str, Any]:
    if _redis_binary is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    try:
        if image.content_type is not None and not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Unsupported content type: {image.content_type}")

        image_bytes: bytes = await image.read()
        _validate_image_bytes(image_bytes)

        media_ref: str = f"media:test:{uuid.uuid4().hex}"
        _redis_binary.setex(media_ref, _MEDIA_TTL_S, image_bytes)

        return {
            "status": "stored",
            "media_ref": media_ref,
            "mime_type": image.content_type or "application/octet-stream",
            "size_bytes": len(image_bytes),
            "ttl_s": _MEDIA_TTL_S,
        }
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc
    finally:
        try:
            await image.close()
        except Exception:
            logger.debug("Failed to close upload")


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    x_turn_hook_signature: str | None = Header(default=None, alias="X-Turn-Hook-Signature"),
) -> dict[str, Any]:
    raw_body = await request.body()
    verify_whatsapp_ingress_auth(
        internal_secret_header=x_internal_secret,
        expected_internal_secret=_WHATSAPP_INGRESS_SECRET,
        raw_body=raw_body,
        turn_signature_header=x_turn_hook_signature,
        turn_webhook_secret=_TURN_WEBHOOK_SECRET,
    )
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="webhook body must be a JSON object")
    payload: dict[str, Any] = parsed

    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    messages = _extract_whatsapp_messages(payload)
    if not messages:
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    normalized_messages: list[OrchestratorIngressRequest] = []
    turn_delegated_count: int = 0
    for message in messages:
        wa_id_raw = str(message.get("from") or "").strip()
        wamid_raw = str(message.get("id") or "").strip()
        if wa_id_raw and not await whatsapp_ingress_should_queue_orchestrator(
            wa_id=wa_id_raw,
            message_id=wamid_raw or None,
        ):
            turn_delegated_count += 1
            continue

        try:
            normalized_messages.append(_normalize_message(message))
        except HTTPException as exc:
            logger.warning("Skipping invalid whatsapp message: detail=%s", exc.detail)

    if turn_delegated_count and not normalized_messages:
        return {
            "status": "delegated_turn",
            "turn_delegated_count": turn_delegated_count,
            "queued_count": 0,
            "request_ids": [],
        }

    if not normalized_messages:
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    await _enforce_ingress_capacity(cost=len(normalized_messages))

    request_ids: list[str] = []
    skipped_duplicates: int = 0

    # dedupe messages to avoid processing the same message multiple times through retries
    # Dedupe is done with the wamid (whatsapp message id) as the key
    try:
        for normalized in normalized_messages:

            # get the wamid (whatsapp message id)
            wamid = str(normalized.message_id or "").strip()

            # get the dedupe key
            dedupe_key = whatsapp_ingress_seen_key(wamid) if wamid else ""
            if wamid and not claim_whatsapp_ingress_message(
                redis_client=_redis,
                message_id=wamid,
                request_id=normalized.request_id,
            ):
                skipped_duplicates += 1
                logger.info(
                    "whatsapp ingress duplicate skipped: message_id=%s request_id=%s",
                    wamid,
                    normalized.request_id,
                )
                continue
            stream_fields = enrich_stream_fields(
                {
                    "request_id": normalized.request_id,
                    "case_id": normalized.case_id,
                    "client_id": normalized.client_id,
                    "message_id": normalized.message_id,
                    "channel": normalized.channel,
                    "payload": _payload_content_json(normalized),
                }
            )
            try:
                msg_id = str(
                    _redis.xadd(
                        _ORCHESTRATOR_JOB_STREAM,
                        stream_fields,
                        maxlen=_WORKERS_STREAM_MAXLEN,
                        approximate=True,
                    )
                )
            except redis.RedisError:
                if dedupe_key:
                    try:
                        _redis.delete(dedupe_key)
                    except redis.RedisError:
                        logger.exception(
                            "failed to clear whatsapp ingress dedupe after xadd error: message_id=%s",
                            wamid,
                        )
                raise
            record_stream_produce(
                stream=_ORCHESTRATOR_JOB_STREAM,
                msg_id=msg_id,
                fields=stream_fields,
                extra_metadata={"worker": "ingress", "flow": "whatsapp_batch"},
            )
            request_ids.append(normalized.request_id)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

    if not request_ids and skipped_duplicates:
        return {
            "status": "already_processed",
            "queued_count": 0,
            "request_ids": [],
        }
    return {"status": "queued", "queued_count": len(request_ids), "request_ids": request_ids}


@router.post("/webhooks/telegram")
async def telegram_webhook(
    payload: dict[str, Any] = Body(
        ...,
        example={
            "update_id": 123456789,
            "message": {
                "message_id": 99,
                "from": {"id": 10001, "is_bot": False, "username": "demo_user"},
                "chat": {"id": 10001, "type": "private"},
                "date": 1713000000,
                "text": "Please diagnose this",
            },
        },
    )
) -> dict[str, Any]:
    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    # Extract message from payload
    message: Dict[str, Any] = _extract_telegram_message(payload)
    if not message:
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    try:
        normalized: OrchestratorIngressRequest = _normalize_telegram_message(message)
    except HTTPException as exc:
        if exc.status_code >= 500:
            logger.exception("Telegram message processing failed: detail=%s", exc.detail)
            raise
        logger.warning("Skipping invalid telegram message: detail=%s", exc.detail)
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    await _enforce_ingress_capacity(cost=1)

    media_group_id = str(message.get("media_group_id") or "").strip()
    if media_group_id:
        slot = {
            "row": {
                "request_id": normalized.request_id,
                "case_id": normalized.case_id,
                "client_id": normalized.client_id,
                "message_id": normalized.message_id,
                "channel": normalized.channel,
                "username": normalized.username or "",
            },
            "payload_json": _payload_content_json(normalized),
        }
        try:
            append_to_media_batch(
                _redis,
                channel="telegram",
                group_key=media_group_id,
                slot=slot,
                quiet_window_s=_TELEGRAM_MEDIA_GROUP_WINDOW_S,
            )
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Redis unavailable") from exc

        asyncio.create_task(
            _flush_telegram_media_group_after_window(
                channel="telegram",
                group_id=media_group_id,
            )
        )
        return {
            "status": "buffered",
            "queued_count": 0,
            "request_ids": [normalized.request_id],
            "group_id": media_group_id,
        }

    try:
        _enqueue_orchestrator_row(normalized=normalized)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

    return {"status": "queued", "queued_count": 1, "request_ids": [normalized.request_id]}


def _require_redis() -> redis.Redis:
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return _redis


async def _verify_qutis_hmac(request: Request) -> bytes:
    raw_body = await request.body()
    verify_hmac(
        raw_body,
        request.headers.get(SIGNATURE_HEADER),
        request.headers.get(TIMESTAMP_HEADER),
    )
    return raw_body


async def _verify_admin_hmac(request: Request) -> None:
    """
    Verify an HMAC-signed admin request.

    These are bodyless GETs, so the signature covers the full path and query
    string. That binds it to the exact page and filters requested and stops a
    captured signature being replayed against a different account.
    """
    if not billing_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    signed = request.url.path
    if request.url.query:
        signed = f"{signed}?{request.url.query}"
    verify_hmac(
        signed.encode("utf-8"),
        request.headers.get(SIGNATURE_HEADER),
        request.headers.get(TIMESTAMP_HEADER),
    )


@router.get("/admin/billing/overview")
async def admin_billing_overview(request: Request, days: int = 30) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    return billing_admin.overview(days=max(1, min(days, 365)))


@router.get("/admin/billing/users")
async def admin_billing_users(
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
    sort: str = "recent",
) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    return billing_admin.list_accounts(limit=limit, cursor=cursor, sort=sort)


@router.get("/admin/billing/users/{account_id}")
async def admin_billing_user_detail(account_id: str, request: Request) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    detail = billing_admin.account_detail(account_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return detail


@router.get("/admin/billing/users/{account_id}/ledger")
async def admin_billing_user_ledger(
    account_id: str, request: Request, limit: int = 50, before_id: int | None = None
) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    return billing_admin.account_ledger(account_id, limit=limit, before_id=before_id)


@router.get("/admin/billing/payments")
async def admin_billing_payments(
    request: Request, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    return billing_admin.list_payments(status=status, limit=limit)


@router.get("/admin/billing/pricing")
async def admin_billing_pricing(request: Request, scope: str | None = None) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    from dermascan.billing import pricing as billing_pricing

    return {"items": billing_pricing.list_current_prices(scope)}


@router.post("/admin/billing/pricing")
async def admin_billing_set_price(request: Request) -> dict[str, Any]:
    """
    Record a new price, effective immediately.

    A POST, so the signature covers the body rather than the path. Prices are
    never updated in place: this inserts a new row and the previous one stays,
    so a charge made last week is still explainable by the rate that was in
    force when it ran.

    ``changed_by`` is the authenticated admin, taken from the dashboard session
    and carried through here for the audit trail.
    """
    if not billing_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    raw_body = await request.body()
    verify_hmac(
        raw_body,
        request.headers.get(SIGNATURE_HEADER),
        request.headers.get(TIMESTAMP_HEADER),
    )

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed payload") from exc

    scope = str(body.get("scope") or "").strip()
    key = str(body.get("key") or "").strip()
    changed_by = str(body.get("changed_by") or "").strip()

    from dermascan.billing import pricing as billing_pricing

    valid_scopes = {
        billing_pricing.SCOPE_JOB_CREDITS,
        billing_pricing.SCOPE_MODEL_TOKEN_RATE,
        billing_pricing.SCOPE_SETTING,
    }
    if scope not in valid_scopes or not key:
        raise HTTPException(status_code=400, detail="scope and key are required")
    if not changed_by:
        raise HTTPException(status_code=400, detail="changed_by is required")

    try:
        value = Decimal(str(body.get("value")))
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail="value must be numeric")

    billing_pricing.set_price(scope, key, value, created_by=changed_by)
    logger.info("price changed: scope=%s key=%s value=%s by=%s", scope, key, value, changed_by)
    return {"status": "ok", "scope": scope, "key": key, "value": str(value)}


@router.get("/admin/billing/reconciliation")
async def admin_billing_reconciliation(request: Request) -> dict[str, Any]:
    await _verify_admin_hmac(request)
    drift = billing_admin.reconciliation_drift()
    return {"drift": drift, "healthy": len(drift) == 0}


@router.post("/webhooks/paystack")
async def paystack_webhook(request: Request) -> dict[str, str]:
    """
    Receive a Paystack payment notification.

    Does as little as possible: verify, record, enqueue, return 200. Anything
    slower risks a timeout, and a non-200 earns 72 hours of Paystack retries.
    Verification against Paystack and credit granting happen in the billing
    worker, off the request path.

    The signature covers the raw request body, so the body is read as bytes and
    never through a parsed FastAPI model — re-serializing changes the digest and
    would reject every webhook.
    """
    if not billing_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    raw_body = await request.body()

    if not verify_paystack_signature(raw_body, request.headers.get(PAYSTACK_SIGNATURE_HEADER)):
        logger.warning("paystack webhook rejected: bad signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Enforced only for live keys: test-mode webhooks are commonly replayed
    # from a local tunnel or curl, and blocking those blocks testing.
    if paystack_is_live():
        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else ""
        )
        if not is_paystack_ip(client_ip):
            logger.warning("paystack webhook rejected: unexpected source ip=%s", client_ip)
            raise HTTPException(status_code=401, detail="Unrecognized source")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed payload") from exc

    event = str(payload.get("event") or "")
    reference = str((payload.get("data") or {}).get("reference") or "")
    dedupe = paystack_dedupe_key(payload)

    try:
        with billing_transaction() as conn:
            inserted = conn.execute(
                """
                INSERT INTO billing.paystack_event (dedupe_key, event, reference, payload, status)
                VALUES (%s, %s, %s, %s, 'received')
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """,
                (dedupe, event, reference or None, Jsonb(payload)),
            ).fetchone()
    except Exception:
        logger.exception("paystack webhook persist failed: reference=%s", reference)
        raise HTTPException(status_code=500, detail="Storage unavailable")

    if inserted is None:
        logger.info("paystack webhook already seen: dedupe=%s", dedupe)
        return {"status": "duplicate"}

    redis_client = _require_redis()
    fields = enrich_stream_fields(
        {"event": event, "reference": reference, "dedupe_key": dedupe}
    )
    redis_client.xadd(
        _BILLING_JOB_STREAM,
        fields,
        maxlen=_WORKERS_STREAM_MAXLEN,
        approximate=True,
    )
    logger.info("paystack webhook queued: event=%s reference=%s", event, reference)
    return {"status": "queued"}


@router.post("/webhooks/escalation")
async def escalation_physician_return_webhook(request: Request) -> dict[str, str]:
    raw_body = await _verify_qutis_hmac(request)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    case_id = str(body.get("case_id") or "").strip()
    derma_notes = body.get("derma_notes")
    if not case_id or not isinstance(derma_notes, dict):
        raise HTTPException(status_code=400, detail="case_id and derma_notes are required")

    required_note_fields = ("physician_id", "reviewed_at_ms", "disposition", "summary", "clinical_notes")
    if any(field not in derma_notes for field in required_note_fields):
        raise HTTPException(status_code=400, detail="derma_notes missing required fields")

    redis_client = _require_redis()
    cache_key = f"escalation:signal_payload:{case_id}"
    cached_raw = redis_client.get(cache_key)
    if not cached_raw:
        raise HTTPException(status_code=404, detail="case not found or expired")

    try:
        payload = json.loads(cached_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="cached payload corrupt") from exc

    payload["derma_notes"] = derma_notes
    mode = str(payload.get("mode") or "diagnose")
    request_id = str(payload.get("request_id") or case_id)
    client_id = str(payload.get("client_id") or "")
    message_id = str(payload.get("message_id") or "")
    channel = str(payload.get("channel") or "")

    redis_client.xadd(
        _ORCHESTRATOR_SIGNAL_STREAM,
        {
            "signal_type": f"{mode}_physician_reviewed",
            "signal_id": uuid.uuid4().hex,
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "channel": channel,
            "source_event_id": "",
            "payload": json.dumps(payload, default=str),
            "notes": "Physician review completed",
        },
        maxlen=_WORKERS_STREAM_MAXLEN,
        approximate=True,
    )
    redis_client.setex(f"escalation:closed:{case_id}", _ESCALATION_CLOSED_TTL_SEC, "1")

    redis_client.xadd(
        _EVENTS_STREAM,
        {
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "channel": channel,
            "agent": "escalation",
            "mode": mode,
            "status": "physician_review_completed",
        },
        maxlen=_WORKERS_STREAM_MAXLEN,
        approximate=True,
    )

    try:
        redis_client.delete(cache_key)
    except redis.RedisError:
        logger.exception("failed to delete cached escalation payload: case_id=%s", case_id)

    return {"status": "accepted"}


@router.get("/escalations/{case_id}/image")
async def escalation_case_image_proxy(case_id: str, request: Request) -> Response:
    # Bodyless GET: sign/verify the request PATH so the signature is bound to this
    # case_id (which lives in the path, not a body) and cannot be replayed across cases.
    verify_hmac(
        request.url.path.encode("utf-8"),
        request.headers.get(SIGNATURE_HEADER),
        request.headers.get(TIMESTAMP_HEADER),
    )
    redis_client = _require_redis()

    if redis_client.exists(f"escalation:closed:{case_id}"):
        raise HTTPException(status_code=410, detail="case review closed")

    blob_path = redis_client.get(f"escalation:image_path:{case_id}")
    if blob_path and not str(blob_path).startswith("client-media/"):
        blob_path = None
    if not blob_path:
        blob_path = resolve_escalation_image_blob_path(
            redis_client,
            image_blob_path="",
            request_id=case_id,
            case_id=case_id,
        )
        if blob_path:
            cache_escalation_image_path(
                redis_client,
                case_id=case_id,
                blob_path=str(blob_path),
            )
    if not blob_path:
        raise HTTPException(status_code=404, detail="image not found")

    try:
        image_bytes = download_client_image_bytes_by_path(str(blob_path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    head = image_bytes[:16]
    media_type = "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        media_type = "image/gif"
    elif len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        media_type = "image/webp"

    return Response(content=image_bytes, media_type=media_type)

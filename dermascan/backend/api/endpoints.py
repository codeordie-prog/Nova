"""
Backend API endpoint drafts for orchestrator request intake.

These are contract-first placeholders and intentionally contain no implementation.
"""

import base64
import binascii
import asyncio
import hmac
import json
import os
import uuid
from typing import Any, Dict
from dotenv import load_dotenv

from fastapi import APIRouter, Body, File, Header, HTTPException, Request, UploadFile
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
from dermascan.backend.api.group_media_helper import (
    append_to_media_batch,
    claim_media_batch,
    enrich_payload_with_group_metadata,
    wait_for_media_batch_quiet,
)
from dermascan.shared.firebase_storage import upload_client_image
from dermascan.shared.logger import get_logger

load_dotenv()

router = APIRouter(prefix="/v1", tags=["orchestrator"])


logger = get_logger(__name__)

_REDIS_URL: str = os.getenv("REDIS_URL", "")
_ORCHESTRATOR_JOB_STREAM: str = os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
_WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
_MEDIA_TTL_S: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
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

# Initialize redis client
_redis = redis.Redis.from_url(_REDIS_URL, decode_responses=True) if _REDIS_URL else None
_redis_binary = redis.Redis.from_url(_REDIS_URL, decode_responses=False) if _REDIS_URL else None


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


def _verify_whatsapp_ingress_secret(header_value: str | None) -> None:
    """
    Verify shared secret for internal WhatsApp webhook ingress.

    Args:
        header_value: Value from `X-Internal-Secret` header.
    """
    expected = _WHATSAPP_INGRESS_SECRET.strip()
    if not expected:
        logger.warning(
            "WHATSAPP_INGRESS_SECRET not configured; allowing /v1/webhooks/whatsapp without internal auth"
        )
        return
    if not header_value or not hmac.compare_digest(header_value, expected):
        raise HTTPException(status_code=401, detail="invalid ingress secret")


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

        if image_b64:
            try:
                image_bytes: bytes = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid image base64") from exc
            _validate_image_bytes(image_bytes)
            source = AttachmentSource(kind="base64", value=image_b64)
            size_bytes: int | None = len(image_bytes)
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

        if image_b64:
            try:
                image_bytes: bytes = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid image base64") from exc
            _validate_image_bytes(image_bytes)
            source = AttachmentSource(kind="base64", value=image_b64)
            size_bytes: int | None = len(image_bytes)
        elif media_ref:
            source = AttachmentSource(kind="blob_ref", value=media_ref)
            size_bytes = None
        elif image_url.lower().startswith(("http://", "https://")):
            source = AttachmentSource(kind="url", value=image_url)
            size_bytes = image_obj.get("file_size")
        else:
            source = AttachmentSource(kind="whatsapp_media_id", value=media_id or "unknown")
            size_bytes = None

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

    return OrchestratorIngressRequest(
        request_id=uuid.uuid4().hex,
        case_id=client_id,
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

                try:
                    download_url = upload_client_image(
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
                    raise HTTPException(
                        status_code=502,
                        detail="Failed to store image",
                    ) from exc

                media_ref = f"media:telegram:{client_id}:{uuid.uuid4().hex}"
                try:
                    _redis.setex(media_ref, _MEDIA_TTL_S, download_url)
                except redis.RedisError as exc:
                    logger.exception(
                        "Redis media URL cache failed: media_ref=%s client_id=%s",
                        media_ref,
                        client_id,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Redis unavailable",
                    ) from exc

                metadata_extras["telegram_media_ref"] = media_ref

                attachments.append(
                    AttachmentItem(
                        id=str(largest_photo.get("file_unique_id") or f"tg-{uuid.uuid4().hex}"),
                        type="image",
                        mime_type=mime_type,
                        filename=None,
                        size_bytes=len(image_bytes),
                        source=AttachmentSource(kind="url", value=download_url),
                    )
                )

    content = MessageContent(text="\n".join([part for part in text_parts if part]).strip() or None, attachments=attachments)
    if not content.text and not content.attachments:
        raise HTTPException(status_code=400, detail="Unsupported or empty Telegram message")

    return OrchestratorIngressRequest(
        request_id=uuid.uuid4().hex,
        case_id=conversation_id,
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
    _redis.xadd(
        _ORCHESTRATOR_JOB_STREAM,
        {
            "request_id": normalized.request_id,
            "case_id": normalized.case_id,
            "client_id": normalized.client_id,
            "message_id": normalized.message_id,
            "channel": normalized.channel,
            "username": normalized.username or "",
            "payload": payload_json,
        },
        maxlen=_WORKERS_STREAM_MAXLEN,
        approximate=True,
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
            _redis.xadd(
                _ORCHESTRATOR_JOB_STREAM,
                {
                    "request_id": str(row.get("request_id") or ""),
                    "case_id": str(row.get("case_id") or ""),
                    "client_id": str(row.get("client_id") or ""),
                    "message_id": str(row.get("message_id") or ""),
                    "channel": str(row.get("channel") or "telegram"),
                    "username": str(row.get("username") or ""),
                    "payload": enriched_payload,
                },
                maxlen=_WORKERS_STREAM_MAXLEN,
                approximate=True,
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
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    payload: dict[str, Any] = Body(
        ...,
        example={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "message": {
                                    "id": "wamid.HBgL.image...",
                                    "from": "254700000000",
                                    "type": "text",
                                    "text": {"body": "Please diagnose this"},
                                    "attachments": [
                                        {
                                            "id": "img-1",
                                            "type": "image",
                                            "media_ref": "media:test:7f4ab7cb6d224c95ab8fd9737c511e7a",
                                            "mime_type": "image/jpeg",
                                            "caption": "Rash on upper arm"
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            ]
        },
    )
) -> dict[str, Any]:
    _verify_whatsapp_ingress_secret(x_internal_secret)

    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    messages = _extract_whatsapp_messages(payload)
    if not messages:
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    normalized_messages: list[OrchestratorIngressRequest] = []
    for message in messages:
        try:
            normalized_messages.append(_normalize_message(message))
        except HTTPException as exc:
            logger.warning("Skipping invalid whatsapp message: detail=%s", exc.detail)

    if not normalized_messages:
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    request_ids: list[str] = []
    try:
        for normalized in normalized_messages:
            _redis.xadd(
                _ORCHESTRATOR_JOB_STREAM,
                {
                    "request_id": normalized.request_id,
                    "case_id": normalized.case_id,
                    "client_id": normalized.client_id,
                    "message_id": normalized.message_id,
                    "channel": normalized.channel,
                    "payload": _payload_content_json(normalized),
                },
                maxlen=_WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
            request_ids.append(normalized.request_id)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

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

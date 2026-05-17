"""
Backend API endpoint drafts for orchestrator request intake.

These are contract-first placeholders and intentionally contain no implementation.
"""

import base64
import binascii
import json
import os
import uuid
from typing import Any, Dict
from dotenv import load_dotenv

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
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
from dermascan.shared.logger import get_logger
from dermascan.shared.payload_log import dumps_compact, dumps_pretty

load_dotenv()

router = APIRouter(prefix="/v1", tags=["orchestrator"])


logger = get_logger(__name__)


def _is_whatsapp_meta_cdn_url(url: str) -> bool:
    """Meta-hosted attachment URLs 401 on anonymous GET; never treat them as fetchable URLs."""
    u = (url or "").strip().lower()
    return "lookaside.fbsbx.com" in u or "fbcdn.net" in u


_REDIS_URL: str = os.getenv("REDIS_URL", "")
_ORCHESTRATOR_JOB_STREAM: str = os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
_WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
_MEDIA_TTL_S: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
_TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_HTTP_TIMEOUT_S: float = float(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "30"))
_TELEGRAM_TLS_VERIFY: str = os.getenv("TELEGRAM_TLS_VERIFY", "true")
_TELEGRAM_CA_BUNDLE: str = os.getenv("TELEGRAM_CA_BUNDLE", "")

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


def _extract_whatsapp_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                if isinstance(msg, dict) and msg:
                    out.append(msg)
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
        mime_type: str = str(image_obj.get("mime_type") or "image/*")
        image_b64: str = str(image_obj.get("base64") or image_obj.get("data_base64") or "")
        raw_image_url: str = str(image_obj.get("url") or "").strip()
        image_url: str = raw_image_url
        meta_cdn_blanked: bool = False
        if image_url and _is_whatsapp_meta_cdn_url(image_url):
            image_url = ""
            meta_cdn_blanked = True

        if image_url:
            source = AttachmentSource(kind="url", value=image_url)
            size_bytes: int | None = image_obj.get("file_size")
        elif image_b64:
            try:
                image_bytes: bytes = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid image base64") from exc
            _validate_image_bytes(image_bytes)
            source = AttachmentSource(kind="base64", value=image_b64)
            size_bytes = len(image_bytes)
        elif media_ref:
            source = AttachmentSource(kind="blob_ref", value=media_ref)
            size_bytes = None
        else:
            source = AttachmentSource(kind="whatsapp_media_id", value=media_id or "unknown")
            size_bytes = None

        logger.info(
            "[NOVA-INGRESS] normalize IMAGE  wamid=%s  raw_url_from_qutis=%s  meta_cdn_blanked=%s  "
            "has_base64=%s  media_ref=%s  media_id=%s  →  source.kind=%s  source.value=%s",
            message_id,
            raw_image_url or "(none)",
            meta_cdn_blanked,
            bool(image_b64),
            media_ref or "(none)",
            media_id or "(none)",
            source.kind,
            (source.value[:120] if source.value else "(empty)"),
        )
        if source.kind == "whatsapp_media_id" and source.value == "unknown":
            logger.warning(
                "[NOVA-INGRESS] wamid=%s  IMAGE has no url / base64 / media_ref / media_id — "
                "diagnose worker will not be able to resolve an image",
                message_id,
            )

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
        mime_type: str = str(document_obj.get("mime_type") or "application/octet-stream")
        document_url: str = str(document_obj.get("url") or "").strip()
        if document_url and _is_whatsapp_meta_cdn_url(document_url):
            document_url = ""

        if document_url:
            doc_source = AttachmentSource(kind="url", value=document_url)
        else:
            doc_source = AttachmentSource(kind="whatsapp_media_id", value=media_id or "unknown")

        attachments.append(
            AttachmentItem(
                id=media_id or f"doc-{uuid.uuid4().hex}",
                type="document",
                mime_type=mime_type,
                filename=document_obj.get("filename"),
                size_bytes=document_obj.get("file_size"),
                source=doc_source,
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

    # Get photos
    photos: list = message.get("photo") or []
    if photos:
        if _redis_binary is None:
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
                
                # Create an image reference and store it as a REDIS HASH
                media_ref = f"media:telegram:{uuid.uuid4().hex}"
                _redis_binary.setex(media_ref, _MEDIA_TTL_S, image_bytes)

                attachments.append(
                    AttachmentItem(
                        id=str(largest_photo.get("file_unique_id") or f"tg-{uuid.uuid4().hex}"),
                        type="image",
                        mime_type=mime_type,
                        filename=None,
                        size_bytes=len(image_bytes),
                        source=AttachmentSource(kind="blob_ref", value=media_ref),
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
        metadata=RequestMetadata(
            extras={
                "telegram_chat_type": str(chat_obj.get("type") or ""),
                "telegram_username": str(from_obj.get("username") or ""),
            }
        ),
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

    return json.dumps(
        {
            "content": {
                "text": content.text,
                "attachments": attachments,
            }
        },
        default=str,
    )


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
    if _redis is None:
        raise HTTPException(status_code=500, detail="REDIS_URL is not configured")

    messages = _extract_whatsapp_messages(payload)
    logger.info(
        "[NOVA-INGRESS] POST /v1/webhooks/whatsapp  messages=%d\nraw_payload_from_qutis=\n%s",
        len(messages),
        dumps_pretty(payload),
    )
    if not messages:
        logger.info("[NOVA-INGRESS] no messages in payload — ignored")
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    request_ids: list[str] = []
    for message in messages:
        try:
            normalized = _normalize_message(message)
        except HTTPException as exc:
            logger.warning("[NOVA-INGRESS] skipping invalid whatsapp message: detail=%s", exc.detail)
            continue

        content_json = _payload_content_json(normalized)
        try:
            _redis.xadd(
                _ORCHESTRATOR_JOB_STREAM,
                {
                    "request_id": normalized.request_id,
                    "case_id": normalized.case_id,
                    "client_id": normalized.client_id,
                    "message_id": normalized.message_id,
                    "channel": normalized.channel,
                    "payload": content_json,
                },
                maxlen=_WORKERS_STREAM_MAXLEN,
                approximate=True,
            )
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Redis unavailable") from exc

        logger.info(
            "[NOVA-INGRESS] enqueued orchestrator job  stream=%s  request_id=%s  case_id=%s  "
            "client_id=%s  message_id=%s\n  job_payload=\n%s",
            _ORCHESTRATOR_JOB_STREAM,
            normalized.request_id,
            normalized.case_id,
            normalized.client_id,
            normalized.message_id,
            dumps_pretty(json.loads(content_json)) if content_json else "{}",
        )
        request_ids.append(normalized.request_id)

    if not request_ids:
        logger.info("[NOVA-INGRESS] no valid messages queued — ignored")
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    logger.info(
        "[NOVA-INGRESS] webhook done — queued_count=%d  request_ids=%s",
        len(request_ids),
        request_ids,
    )
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
        # Normalize telegram messgae - Returns The pydantic model for Orchestrator ingress request
        normalized: OrchestratorIngressRequest = _normalize_telegram_message(message)
    except HTTPException as exc:
        logger.warning("Skipping invalid telegram message: detail=%s", exc.detail)
        return {"status": "ignored", "queued_count": 0, "request_ids": []}

    try:
        # Add normalized message to REDIS stream
        _redis.xadd(
            _ORCHESTRATOR_JOB_STREAM,
            {
                "request_id": normalized.request_id,
                "case_id": normalized.case_id,
                "client_id": normalized.client_id,
                "message_id": normalized.message_id,
                "channel": normalized.channel,
                "username": normalized.username or "",
                "payload": _payload_content_json(normalized),
            },
            maxlen=_WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

    return {"status": "queued", "queued_count": 1, "request_ids": [normalized.request_id]}

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-24
Licence: PolyForm Noncommercial License 1.0.0

Turn.io WhatsApp sidecar adapter for Dermascan outbound (and optional inbound relay).

Implements the qutis sidecar contract expected by ``outbound_sender_worker.py``:

- ``POST /send`` — ``{ "to", "kind", "text", "reply_to_wamid"? }`` + ``X-Internal-Secret``
- ``POST /typing`` — ``{ "message_id" }`` + ``X-Internal-Secret``

Translates those calls to Turn's Messages API (``POST /v1/messages``, ``PUT /v1/messages/{id}``).

Optional inbound relay (production-shaped): when ``NOVA_WEBHOOK_URL`` is set, Turn webhooks
posted to this process are signature-verified and forwarded to Nova with ``X-Internal-Secret``.

Run (outbound only; Nova on 8088, ngrok → Nova):

    poetry run uvicorn turn.scripts.turn_sidecar_adapter:app --host 0.0.0.0 --port 8000

Env (see ``.env`` / ``bootstrap_env``):

- ``TURN_API_TOKEN`` — required for outbound
- ``TURN_API_BASE_URL`` — default ``https://whatsapp.turn.io``
- ``WHATSAPP_INTERNAL_API_SECRET`` or ``INTERNAL_API_SECRET`` — sidecar auth (must match worker)
- ``NOVA_WEBHOOK_URL`` — optional, e.g. ``http://127.0.0.1:8088/v1/webhooks/whatsapp``
- ``TURN_WEBHOOK_SECRET`` — required when ``NOVA_WEBHOOK_URL`` is set (verify Turn HMAC)
- ``WHATSAPP_INGRESS_SECRET`` — injected on forward to Nova (falls back to internal secret)
"""


from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dermascan.shared.load_env import bootstrap_env
from dermascan.shared.turn_webhook_auth import turn_webhook_signature_valid

bootstrap_env()

logger: logging.Logger = logging.getLogger("turn-sidecar-adapter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

TURN_API_BASE_URL: str = os.getenv("TURN_API_BASE_URL", "https://whatsapp.turn.io").rstrip("/")
TURN_API_TOKEN: str = os.getenv("TURN_API_TOKEN", "").strip()
TURN_WEBHOOK_SECRET: str = os.getenv("TURN_WEBHOOK_SECRET", "").strip()
_DEFAULT_NOVA_WEBHOOK_URL: str = "http://127.0.0.1:8088/v1/webhooks/whatsapp"
NOVA_WEBHOOK_URL: str = (
    os.getenv("NOVA_WEBHOOK_URL", _DEFAULT_NOVA_WEBHOOK_URL).strip() or _DEFAULT_NOVA_WEBHOOK_URL
)
WHATSAPP_INTERNAL_API_SECRET: str = os.getenv(
    "WHATSAPP_INTERNAL_API_SECRET",
    os.getenv("INTERNAL_API_SECRET", ""),
).strip()
WHATSAPP_INGRESS_SECRET: str = os.getenv(
    "WHATSAPP_INGRESS_SECRET",
    WHATSAPP_INTERNAL_API_SECRET,
).strip()
TURN_HTTP_TIMEOUT_S: float = float(os.getenv("TURN_HTTP_TIMEOUT_S", "20"))

_TURN_ACCEPT = "application/vnd.v1+json"

app = FastAPI(title="Turn.io sidecar adapter", version="1.0.0")


class SendRequest(BaseModel):
    """Sidecar ``/send`` body."""

    to: str = Field(..., description="Recipient wa_id (E.164 digits, optional leading +).")
    kind: str = Field(default="text", description="Message kind: ``text`` or ``audio``.")
    text: str = Field(default="", description="Plain text body when kind is text.")
    audio_b64: str | None = Field(default=None, description="Base64 audio when kind is audio.")
    audio_mime_type: str = Field(default="audio/ogg", description="MIME type for audio upload.")
    reply_to_wamid: str | None = Field(default=None, description="Optional inbound wamid for threading.")


class TypingRequest(BaseModel):
    """Sidecar ``/typing`` body."""

    message_id: str = Field(..., description="Inbound WhatsApp message id (wamid).")


class SidecarSendResponse(BaseModel):
    """Response shape required by ``outbound_sender_worker._send_whatsapp``."""

    status: str = "sent"
    message_id: str | None = None


def _normalize_wa_id(raw: str) -> str:
    """
    Normalize a WhatsApp recipient id to digits without a leading plus.

    Args:
        raw: Raw ``to`` or case_id value.

    Returns:
        Digits-only wa_id.
    """
    normalized = str(raw or "").strip()
    if normalized.startswith("+"):
        normalized = normalized[1:]
    return normalized


def _verify_internal_secret(header_value: str | None) -> None:
    """
    Verify ``X-Internal-Secret`` for sidecar endpoints.

    Args:
        header_value: Header from the inbound HTTP request.

    Raises:
        HTTPException: When secret is configured and the header does not match.
    """
    expected = WHATSAPP_INTERNAL_API_SECRET
    if not expected:
        logger.warning("WHATSAPP_INTERNAL_API_SECRET unset; allowing /send and /typing without auth")
        return
    if not header_value or not hmac.compare_digest(header_value, expected):
        raise HTTPException(status_code=401, detail="invalid internal secret")


def _turn_auth_headers() -> dict[str, str]:
    """
    Build headers for Turn REST calls.

    Returns:
        Authorization and content negotiation headers.

    Raises:
        HTTPException: When ``TURN_API_TOKEN`` is missing.
    """
    if not TURN_API_TOKEN:
        raise HTTPException(status_code=503, detail="TURN_API_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {TURN_API_TOKEN}",
        "Accept": _TURN_ACCEPT,
        "Content-Type": "application/json",
    }


def _turn_signature_valid(raw_body: bytes, signature: str | None) -> bool:
    """
    Verify Turn webhook HMAC-SHA256 signature.

    Args:
        raw_body: Raw request body bytes.
        signature: ``X-Turn-Hook-Signature`` header value.

    Returns:
        True if signature matches, False otherwise.
    """
    return turn_webhook_signature_valid(
        raw_body=raw_body,
        signature=signature,
        webhook_secret=TURN_WEBHOOK_SECRET,
    )


async def _upload_turn_media(
    client: httpx.AsyncClient,
    *,
    audio_bytes: bytes,
    mime_type: str,
) -> str:
    """
    Upload binary media to Turn ``POST /v1/media``.

    Args:
        client: Shared HTTP client.
        audio_bytes: Raw file bytes.
        mime_type: Content-Type for the upload (e.g. ``audio/ogg``).

    Returns:
        str: Turn media id.

    Raises:
        HTTPException: When upload fails or response lacks an id.
    """
    url = f"{TURN_API_BASE_URL}/v1/media"
    headers = _turn_auth_headers()
    headers["Content-Type"] = mime_type
    response = await client.post(url, headers=headers, content=audio_bytes)
    if response.status_code >= 400:
        logger.error(
            "Turn media upload failed: HTTP %s mime=%s body=%s",
            response.status_code,
            mime_type,
            response.text[:300],
        )
        raise HTTPException(
            status_code=502,
            detail=f"turn media upload HTTP {response.status_code}: {response.text[:200]}",
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="turn media upload returned non-JSON") from exc

    media_list = payload.get("media")
    if isinstance(media_list, list) and media_list:
        media_id = str((media_list[0] or {}).get("id") or "").strip()
        if media_id:
            return media_id
    media_id = str(payload.get("id") or "").strip()
    if media_id:
        return media_id
    raise HTTPException(status_code=502, detail="turn media upload missing id")


def _build_turn_audio_message(body: SendRequest, media_id: str) -> tuple[dict[str, Any], str | None]:
    """
    Map sidecar audio send payload to Turn Messages API JSON.

    Args:
        body: Validated sidecar request with ``audio_b64`` already uploaded.
        media_id: Turn media id from ``_upload_turn_media``.

    Returns:
        Tuple of Turn message JSON and optional reply wamid.
    """
    to_wa = _normalize_wa_id(body.to)
    if not to_wa:
        raise HTTPException(status_code=400, detail="missing or invalid to")

    message: dict[str, Any] = {
        "recipient_type": "individual",
        "to": to_wa,
        "type": "audio",
        "audio": {"id": media_id},
    }
    reply_wamid = str(body.reply_to_wamid or "").strip() or None
    return message, reply_wamid


def _build_turn_text_message(body: SendRequest) -> tuple[dict[str, Any], str | None]:
    """
    Map sidecar send payload to Turn Messages API JSON.

    Args:
        body: Validated sidecar request.

    Returns:
        Tuple of base Turn message JSON and optional inbound wamid for quoting.

    Raises:
        HTTPException: For unsupported kinds or missing recipient.
    """
    if body.kind.strip().lower() != "text":
        raise HTTPException(status_code=501, detail=f"unsupported kind: {body.kind!r}")

    to_wa = _normalize_wa_id(body.to)
    if not to_wa:
        raise HTTPException(status_code=400, detail="missing or invalid to")

    text_body = str(body.text or "")
    if not text_body.strip():
        raise HTTPException(status_code=400, detail="empty text")

    message: dict[str, Any] = {
        "recipient_type": "individual",
        "to": to_wa,
        "type": "text",
        "text": {"body": text_body},
    }
    reply_wamid = str(body.reply_to_wamid or "").strip() or None
    return message, reply_wamid


async def _post_turn_text_message(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    base_body: dict[str, Any],
    reply_wamid: str | None,
) -> httpx.Response:
    """
    POST a text message to Turn, optionally as a quoted reply (WhatsApp thread).

    Uses Meta Cloud API ``context.message_id`` for visible threading. Some Turn
    environments reject ``context``; on HTTP 400 we retry without it so delivery
    still succeeds.

    Args:
        client: Shared HTTP client.
        url: Turn ``/v1/messages`` URL.
        headers: Authorization and content headers.
        base_body: Message JSON without reply context.
        reply_wamid: Inbound wamid to quote, if any.

    Returns:
        httpx.Response from Turn (last attempt).
    """
    if not reply_wamid:
        return await client.post(url, headers=headers, json=base_body)

    headers_with_audit = {**headers, "X-Turn-In-Reply-To": reply_wamid}
    attempts: list[dict[str, Any]] = [
        {**base_body, "context": {"message_id": reply_wamid}},
        {**base_body, "context": {"id": reply_wamid}},
    ]
    last_response: httpx.Response | None = None
    for index, body in enumerate(attempts):
        last_response = await client.post(url, headers=headers_with_audit, json=body)
        if last_response.status_code < 400:
            return last_response
        detail = last_response.text.lower()
        if last_response.status_code == 400 and "invalid key" in detail:
            logger.warning(
                "Turn rejected reply context attempt=%d reply_to=%s body=%s",
                index + 1,
                reply_wamid[:32],
                last_response.text[:200],
            )
            continue
        return last_response

    logger.warning(
        "Turn rejected all reply context shapes for reply_to=%s; sending without quote",
        reply_wamid[:32],
    )
    assert last_response is not None
    fallback = await client.post(url, headers=headers_with_audit, json=base_body)
    if fallback.status_code < 400:
        return fallback
    return last_response


async def _forward_turn_webhook_to_nova(
    raw_body: bytes,
    subscription: str | None,
    signature: str | None,
) -> dict[str, Any]:
    """
    Verify Turn signature and proxy webhook payload to Nova ingress.

    Args:
        raw_body: Raw Turn webhook body.
        subscription: ``X-Turn-Hook-Subscription`` value.
        signature: ``X-Turn-Hook-Signature`` value.

    Returns:
        JSON parsed from Nova's response when possible.

    Raises:
        HTTPException: On auth failure or upstream errors.
    """
    if TURN_WEBHOOK_SECRET:
        if not _turn_signature_valid(raw_body, signature):
            raise HTTPException(status_code=401, detail="invalid Turn webhook signature")
    else:
        logger.warning(
            "TURN_WEBHOOK_SECRET unset; forwarding webhook to Nova without signature verification"
        )

    forward_headers: dict[str, str] = {"Content-Type": "application/json"}
    if WHATSAPP_INGRESS_SECRET:
        forward_headers["X-Internal-Secret"] = WHATSAPP_INGRESS_SECRET
    if subscription:
        forward_headers["X-Turn-Hook-Subscription"] = subscription
    if signature:
        forward_headers["X-Turn-Hook-Signature"] = signature

    async with httpx.AsyncClient(timeout=TURN_HTTP_TIMEOUT_S) as client:
        response = await client.post(NOVA_WEBHOOK_URL, content=raw_body, headers=forward_headers)

    if response.status_code >= 400:
        logger.error(
            "Nova webhook forward failed: HTTP %s body=%s",
            response.status_code,
            response.text[:300],
        )
        raise HTTPException(status_code=502, detail=f"nova ingress HTTP {response.status_code}")

    if response.content:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"ack": True, "raw": response.text[:200]}
    return {"ack": True}


@app.get("/")
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "turn-sidecar-adapter"}


@app.post("/send", response_model=SidecarSendResponse)
async def send_message(
    body: SendRequest,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> SidecarSendResponse:
    """
    Send a WhatsApp text message via Turn (sidecar contract).

    Args:
        body: Sidecar send payload.
        x_internal_secret: Shared secret from the outbound worker.

    Returns:
        ``{"status": "sent", "message_id": ...}`` for the outbound worker.

    Raises:
        HTTPException: On auth, validation, or Turn API errors.
    """
    _verify_internal_secret(x_internal_secret)
    kind = body.kind.strip().lower()
    url = f"{TURN_API_BASE_URL}/v1/messages"
    post_headers = _turn_auth_headers()

    async with httpx.AsyncClient(timeout=TURN_HTTP_TIMEOUT_S) as client:
        if kind == "audio":
            audio_b64 = str(body.audio_b64 or "").strip()
            if not audio_b64:
                raise HTTPException(status_code=400, detail="missing audio_b64 for audio kind")
            try:
                audio_bytes = base64.b64decode(audio_b64, validate=True)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="invalid audio_b64") from exc
            mime_type = str(body.audio_mime_type or "audio/ogg").strip() or "audio/ogg"
            media_id = await _upload_turn_media(client, audio_bytes=audio_bytes, mime_type=mime_type)
            turn_body, reply_wamid = _build_turn_audio_message(body, media_id)
            response = await _post_turn_text_message(
                client,
                url=url,
                headers=post_headers,
                base_body=turn_body,
                reply_wamid=reply_wamid,
            )
        elif kind == "text":
            turn_body, reply_wamid = _build_turn_text_message(body)
            response = await _post_turn_text_message(
                client,
                url=url,
                headers=post_headers,
                base_body=turn_body,
                reply_wamid=reply_wamid,
            )
        else:
            raise HTTPException(status_code=501, detail=f"unsupported kind: {body.kind!r}")

    if response.status_code >= 400:
        logger.error(
            "Turn send failed: HTTP %s to=%s reply_to=%s body=%s",
            response.status_code,
            turn_body.get("to"),
            reply_wamid or "",
            response.text[:400],
        )
        raise HTTPException(
            status_code=502,
            detail=f"turn messages API HTTP {response.status_code}: {response.text[:200]}",
        )

    message_id: str | None = None
    try:
        payload = response.json()
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            message_id = str(messages[0].get("id") or "") or None
    except json.JSONDecodeError:
        logger.warning("Turn send returned non-JSON body")

    logger.info("OUTBOUND sent to=%s wamid=%s", turn_body.get("to"), message_id)
    return SidecarSendResponse(status="sent", message_id=message_id)


@app.post("/typing")
async def send_typing(
    body: TypingRequest,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> dict[str, str]:
    """
    Best-effort typing indicator via Turn (Cloud API read + typing).

    Args:
        body: Sidecar typing payload with inbound ``message_id``.
        x_internal_secret: Shared secret from the outbound worker.

    Returns:
        Simple acknowledgement (failures are logged, not raised, to match worker behaviour).

    Raises:
        HTTPException: Only on auth failure.
    """
    _verify_internal_secret(x_internal_secret)
    wamid = str(body.message_id or "").strip()
    if not wamid:
        return {"status": "skipped", "reason": "missing message_id"}

    url = f"{TURN_API_BASE_URL}/v1/messages/{wamid}"
    turn_body = {
        "status": "read",
        "typing_indicator": {"type": "text"},
    }

    try:
        async with httpx.AsyncClient(timeout=min(TURN_HTTP_TIMEOUT_S, 5.0)) as client:
            response = await client.put(url, headers=_turn_auth_headers(), json=turn_body)
        if response.status_code >= 400:
            logger.debug("Turn typing failed: HTTP %s %s", response.status_code, response.text[:200])
            return {"status": "failed", "reason": f"turn HTTP {response.status_code}"}
    except httpx.HTTPError as exc:
        logger.debug("Turn typing error: %s", exc)
        return {"status": "failed", "reason": "http error"}

    return {"status": "ok"}


@app.post("/")
@app.post("/v1/webhooks/whatsapp")
@app.post("/webhook")
async def turn_webhook_ingress(
    request: Request,
    x_turn_hook_subscription: str | None = Header(default=None, alias="X-Turn-Hook-Subscription"),
    x_turn_hook_signature: str | None = Header(default=None, alias="X-Turn-Hook-Signature"),
) -> dict[str, Any]:
    """
    Turn → Nova webhook relay (default target ``NOVA_WEBHOOK_URL`` → local Nova :8088).

    Args:
        request: Raw Turn webhook request.
        x_turn_hook_subscription: Turn subscription header.
        x_turn_hook_signature: Turn HMAC signature header.

    Returns:
        Nova ingress JSON response or a simple ack.

    Raises:
        HTTPException: When signature verification fails or Nova returns an error.
    """
    raw = await request.body()
    logger.info(
        "INBOUND relay sub=%s bytes=%d → %s",
        x_turn_hook_subscription or "(none)",
        len(raw),
        NOVA_WEBHOOK_URL,
    )
    return await _forward_turn_webhook_to_nova(
        raw,
        x_turn_hook_subscription,
        x_turn_hook_signature,
    )

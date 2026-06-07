"""
Author: Dermascan Team
Date: 2026-04-25

Description: Diagnose worker. Consumes diagnose jobs and deterministically dispatches
prompt + image to the MedGemma gateway. No LLM is involved in the dispatch path.
"""

import base64
import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import httpx
import redis
from dotenv import load_dotenv

from dermascan.shared.firebase_storage import download_client_image_bytes
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)
from dermascan.backend.services.agents import reclaim_logic

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "")
MEDGEMMA_GATEWAY_URL: str = os.getenv("MEDGEMMA_GATEWAY_URL", "http://127.0.0.1:8080")
MEDGEMMA_AUTH_HEADER: str = os.getenv("MEDGEMMA_AUTH_HEADER", "")
MEDGEMMA_AUTH_VALUE: str = os.getenv("MEDGEMMA_AUTH_VALUE", "")
MEDGEMMA_HTTP_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_HTTP_TIMEOUT_S", "60"))

# Retry configuration for HTTP requests
MEDGEMMA_HTTP_MAX_RETRIES: int = max(0, int(os.getenv("MEDGEMMA_HTTP_MAX_RETRIES", "3")))
MEDGEMMA_HTTP_RETRY_DELAY_MS: int = max(0, int(os.getenv("MEDGEMMA_HTTP_RETRY_DELAY_MS", "200")))
MEDGEMMA_HTTP_RETRY_BACKOFF_MULTIPLIER: float = float(os.getenv("MEDGEMMA_HTTP_RETRY_BACKOFF_MULTIPLIER", "2.0"))

DIAGNOSE_JOB_STREAM: str = os.getenv("DIAGNOSE_JOB_STREAM", "diagnose:jobs")
DIAGNOSE_CONSUMER_GROUP: str = os.getenv("DIAGNOSE_CONSUMER_GROUP", "diagnose-workers")
DIAGNOSE_CONSUMER_NAME_BASE: str = os.getenv("DIAGNOSE_CONSUMER_NAME", "diagnose-worker")
DIAGNOSE_CONSUMER_NAME: str = f"{DIAGNOSE_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
DIAGNOSE_DLQ_STREAM: str = os.getenv("DIAGNOSE_DLQ_STREAM", "diagnose:dlq")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
WORKERS_STREAM_BLOCK_MS: int = int(os.getenv("WORKERS_STREAM_BLOCK_MS", "500"))
WORKERS_STREAM_BATCH_SIZE: int = int(os.getenv("WORKERS_STREAM_BATCH_SIZE", "1"))
WORKERS_MAX_INFLIGHT: int = max(1, int(os.getenv("WORKERS_MAX_INFLIGHT", "8")))

DEFAULT_DIAGNOSE_PROMPT: str = os.getenv(
    "DIAGNOSE_DEFAULT_PROMPT",
    "Provide a structured dermatological assessment of the provided image.",
)

_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
_IMAGE_FETCH_TIMEOUT_S: float = float(os.getenv("IMAGE_FETCH_TIMEOUT_S", "30"))
WHATSAPP_API_BASE_URL: str = os.getenv("WHATSAPP_API_BASE_URL", "http://127.0.0.1:8000")
WHATSAPP_MEDIA_PATH: str = os.getenv("WHATSAPP_MEDIA_PATH", "/internal/whatsapp-media")
WHATSAPP_MEDIA_HTTP_TIMEOUT_S: float = float(
    os.getenv("WHATSAPP_MEDIA_HTTP_TIMEOUT_S", str(_IMAGE_FETCH_TIMEOUT_S))
)
WHATSAPP_TLS_VERIFY: str = os.getenv("WHATSAPP_TLS_VERIFY", "true")
WHATSAPP_CA_BUNDLE: str = os.getenv("WHATSAPP_CA_BUNDLE", "")
WHATSAPP_INTERNAL_API_SECRET: str = os.getenv(
    "WHATSAPP_INTERNAL_API_SECRET",
    os.getenv("INTERNAL_API_SECRET", ""),
)

logger: logging.Logger = get_logger("agents")

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is required for the diagnose worker")

_redis_text: redis.Redis = get_redis_client(decode_responses=True)
_redis_binary: redis.Redis = get_redis_client(decode_responses=False)

# Initialize reclaim_logic with the Redis client
reclaim_logic.set_redis_client(_redis_text)


def _ensure_consumer_group() -> None:
    """
    Ensure the consumer group exists for the diagnose job stream.
    """
    try:
        _redis_text.xgroup_create(
            DIAGNOSE_JOB_STREAM, DIAGNOSE_CONSUMER_GROUP, id="0", mkstream=True
        )
        logger.info(
            "diagnose consumer group created: stream=%s group=%s",
            DIAGNOSE_JOB_STREAM,
            DIAGNOSE_CONSUMER_GROUP,
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _emit_event(fields: dict[str, Any]) -> None:
    """
    Emit a diagnostic event to the events stream.
    
    Args:
        fields: Event fields to emit
    """
    try:
        _redis_text.xadd(
            EVENTS_STREAM,
            {k: ("" if v is None else str(v)) for k, v in fields.items()},
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("failed to append diagnose event")


def _is_http_url(value: str) -> bool:
    """
    Determine if a value is a HTTP URL.

    Args:
        value: The value to check

    Returns:
        True if the value is a HTTP URL, False otherwise
    """
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _validate_image_bytes(image_bytes: bytes) -> bool:
    """
    Validate image bytes.

    Args:
        image_bytes: The image bytes to validate

    Returns:
        True if the image bytes are valid, False otherwise
    """
    if not image_bytes or len(image_bytes) > _MAX_IMAGE_BYTES:
        return False

    head = image_bytes[:16]
    is_jpeg = head.startswith(b"\xff\xd8\xff")
    is_png = head.startswith(b"\x89PNG\r\n\x1a\n")
    is_gif = head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    is_webp = len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"
    return is_jpeg or is_png or is_gif or is_webp


def _is_firebase_storage_url(url: str) -> bool:
    """
    Determine if a value is a Firebase Storage URL.

    Args:
        url: The URL to check

    Returns:
        True if the URL is a Firebase Storage URL, False otherwise
    """
    lowered = url.strip().lower()
    return (
        "storage.googleapis.com" in lowered
        or "firebasestorage.googleapis.com" in lowered
        or lowered.startswith("gs://")
    )


def _fetch_image_bytes_from_url(url: str) -> bytes:
    """
    Fetch image bytes from a URL.

    Args:
        url: The URL to fetch the image from

    Returns:
        The image bytes
    """
    if _is_firebase_storage_url(url):
        image_bytes = download_client_image_bytes(url=url)
        if not _validate_image_bytes(image_bytes):
            raise ValueError(f"invalid or oversized image from firebase url: {url}")
        return image_bytes

    response = httpx.get(
        url,
        timeout=_IMAGE_FETCH_TIMEOUT_S,
        follow_redirects=True,
    )
    response.raise_for_status()
    image_bytes = response.content
    if not _validate_image_bytes(image_bytes):
        raise ValueError(f"invalid or oversized image from url: {url}")
    return image_bytes


def _resolve_blob_ref(media_ref: str) -> bytes:
    """
    Resolve image bytes from a Redis media reference.

    New refs store a download URL (text). Legacy test refs may store raw bytes.
    """
    cached_url = _redis_text.get(media_ref)
    if cached_url:
        if _is_http_url(cached_url):
            return _fetch_image_bytes_from_url(cached_url)
        logger.warning("blob_ref redis value is not a URL: %s", media_ref)
        return b""

    raw = _redis_binary.get(media_ref)
    if raw and _validate_image_bytes(raw):
        return raw

    return b""


def _resolve_whatsapp_media_id(media_id: str) -> bytes:
    """
    Resolve image bytes from WhatsApp media id via qutis internal media endpoint.

    Args:
        media_id: WhatsApp media identifier from webhook payload.

    Returns:
        Image bytes if resolution succeeds, otherwise empty bytes.
    """
    mid = str(media_id or "").strip()
    if not mid:
        return b""

    secret = WHATSAPP_INTERNAL_API_SECRET.strip()
    if not secret:
        logger.warning("whatsapp media resolve skipped: missing WHATSAPP_INTERNAL_API_SECRET")
        return b""

    verify: bool | str = True
    if WHATSAPP_CA_BUNDLE.strip():
        verify = WHATSAPP_CA_BUNDLE.strip()
    elif WHATSAPP_TLS_VERIFY.strip().lower() in {"0", "false", "no", "off"}:
        verify = False

    base_url = WHATSAPP_API_BASE_URL.rstrip("/")
    media_path = WHATSAPP_MEDIA_PATH if WHATSAPP_MEDIA_PATH.startswith("/") else f"/{WHATSAPP_MEDIA_PATH}"
    media_url = f"{base_url}{media_path}/{mid}"
    headers = {"X-Internal-Secret": secret}

    response = httpx.get(
        media_url,
        headers=headers,
        timeout=WHATSAPP_MEDIA_HTTP_TIMEOUT_S,
        follow_redirects=True,
        verify=verify,
    )
    response.raise_for_status()
    image_bytes = response.content
    if not _validate_image_bytes(image_bytes):
        raise ValueError(f"invalid or oversized image from whatsapp media id: {mid}")
    return image_bytes


def _telegram_media_ref_from_payload(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    extras = metadata.get("extras") or {}
    return str(extras.get("telegram_media_ref") or "").strip()


def _resolve_image(payload: dict[str, Any]) -> tuple[bytes, str, str]:
    """
    Resolve image bytes from the diagnose payload.

    Returns:
        (image_bytes, mime_type, source_kind). image_bytes is empty when no
        image was found.
    """
    content = payload.get("content") or {}
    attachments = content.get("attachments") or []
    telegram_media_ref = _telegram_media_ref_from_payload(payload)

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = str(attachment.get("type") or "")
        mime_type = str(attachment.get("mime_type") or "image/jpeg")
        if attachment_type != "image" and not mime_type.startswith("image/"):
            continue

        source = attachment.get("source") or {}
        kind = str(source.get("kind") or "")
        value = str(source.get("value") or "")
        if not value:
            continue

        if kind == "base64":
            try:
                image_bytes = base64.b64decode(value, validate=True)
                if _validate_image_bytes(image_bytes):
                    return image_bytes, mime_type, kind
            except Exception:
                logger.exception("invalid base64 attachment")
                continue

        if kind == "url" and _is_http_url(value):
            try:
                return _fetch_image_bytes_from_url(value), mime_type, kind
            except Exception:
                logger.exception("failed to fetch image from attachment url: %s", value)
                if telegram_media_ref:
                    try:
                        image_bytes = _resolve_blob_ref(telegram_media_ref)
                        if image_bytes:
                            return image_bytes, mime_type, "blob_ref"
                    except Exception:
                        logger.exception(
                            "failed to resolve telegram_media_ref fallback: %s",
                            telegram_media_ref,
                        )
                continue

        if kind == "blob_ref":
            try:
                image_bytes = _resolve_blob_ref(value)
                if image_bytes:
                    return image_bytes, mime_type, kind
            except Exception:
                logger.exception("failed to resolve blob_ref: %s", value)
            logger.warning("media_ref not found or expired: %s", value)
            continue

        if kind == "whatsapp_media_id":
            try:
                image_bytes = _resolve_whatsapp_media_id(value)
                if image_bytes:
                    return image_bytes, mime_type, kind
            except Exception:
                logger.exception("failed to resolve whatsapp media id: %s", value)
            logger.warning("whatsapp media id not resolvable: %s", value)
            continue

    if telegram_media_ref:
        try:
            image_bytes = _resolve_blob_ref(telegram_media_ref)
            if image_bytes:
                return image_bytes, "image/jpeg", "blob_ref"
        except Exception:
            logger.exception(
                "failed to resolve telegram_media_ref without attachments: %s",
                telegram_media_ref,
            )

    return b"", "image/jpeg", ""


def _is_retryable_status_code(status_code: int) -> bool:
    """
    Determine if an HTTP status code is retryable.

    Args:
        status_code: The HTTP status code

    Returns:
        True if the status code is retryable, False otherwise
    """
    return status_code in (429, 502, 503, 504)


def _post_with_retry(
    endpoint: str,
    json_data: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """
    Post to an endpoint with retry logic for transient errors.

    Args:
        endpoint: The URL to post to
        json_data: The JSON data to send
        headers: The headers to send
        timeout: The timeout in seconds

    Returns:
        The response JSON data

    Raises:
        httpx.HTTPStatusError: If the request fails after all retries
        httpx.HTTPError: For other HTTP errors
    """
    last_exception: Exception | None = None

    for attempt in range(MEDGEMMA_HTTP_MAX_RETRIES + 1):
        try:
            response = httpx.post(
                endpoint,
                json=json_data,
                headers=headers or None,
                timeout=timeout,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json() if response.content else {}
            data["_endpoint"] = endpoint
            return data

        except httpx.HTTPStatusError as exc:
            last_exception = exc
            status_code = exc.response.status_code if exc.response is not None else 0

            if not _is_retryable_status_code(status_code):
                # Non-retryable error, raise immediately
                raise

            if attempt >= MEDGEMMA_HTTP_MAX_RETRIES:
                # Max retries exhausted
                raise

            # Calculate delay with exponential backoff
            delay_ms = MEDGEMMA_HTTP_RETRY_DELAY_MS * (MEDGEMMA_HTTP_RETRY_BACKOFF_MULTIPLIER ** attempt)

            # Respect Retry-After header if present
            if exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay_ms = max(delay_ms, int(float(retry_after) * 1000))
                    except (ValueError, TypeError):
                        pass

            logger.warning(
                "http request failed (retryable): status=%s attempt=%s/%s delay_ms=%s endpoint=%s",
                status_code, attempt + 1, MEDGEMMA_HTTP_MAX_RETRIES + 1, delay_ms, endpoint,
            )
            time.sleep(delay_ms / 1000.0)

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_exception = exc

            if attempt >= MEDGEMMA_HTTP_MAX_RETRIES:
                raise

            delay_ms = MEDGEMMA_HTTP_RETRY_DELAY_MS * (MEDGEMMA_HTTP_RETRY_BACKOFF_MULTIPLIER ** attempt)
            logger.warning(
                "http request failed (network error): attempt=%s/%s delay_ms=%s endpoint=%s error=%s",
                attempt + 1, MEDGEMMA_HTTP_MAX_RETRIES + 1, delay_ms, endpoint, str(exc),
            )
            time.sleep(delay_ms / 1000.0)

    # This should never be reached, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected error in retry logic")


def _post_to_medgemma_base64(
    image_bytes: bytes,
    prompt: str,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    group_id: str = "",
    group_expected_count: int = 0,
) -> dict[str, Any]:
    """
    Post the image and prompt to the MedGemma gateway.

    Args:
        image_bytes: The image bytes to send
        prompt: The prompt to send
        request_id: The request ID
        case_id: The case ID
        client_id: The client ID
        message_id: The message ID
        channel: The channel

    Returns:
        The response from the MedGemma gateway
    """
    endpoint = f"{MEDGEMMA_GATEWAY_URL.rstrip('/')}/diagnose_base64"
    headers: dict[str, str] = {}

    # Authentication
    if MEDGEMMA_AUTH_HEADER and MEDGEMMA_AUTH_VALUE:
        headers[MEDGEMMA_AUTH_HEADER] = MEDGEMMA_AUTH_VALUE

    # Encode raw bytes to base64
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    return _post_with_retry(
        endpoint=endpoint,
        json_data={
            "image_base64": image_b64,
            "prompt": prompt,
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "channel": channel,
            "group_id": group_id,
            "group_expected_count": group_expected_count,
        },
        headers=headers,
        timeout=MEDGEMMA_HTTP_TIMEOUT_S,
    )


def _process_stream_message(msg_id: str, fields: dict[str, Any]) -> None:
    """
    Process a message from the diagnose job stream.
    
    Args:
        msg_id: The message ID
        fields: The message fields
    """

    # Extract fields
    request_id = str(fields.get("request_id") or "")
    case_id = str(fields.get("case_id") or request_id)
    client_id = str(fields.get("client_id") or "anonymous")
    message_id = str(fields.get("message_id") or request_id)
    channel = str(fields.get("channel") or "whatsapp")
    payload_json = str(fields.get("payload") or "{}")

    try:
        try:
            payload: dict[str, Any] = json.loads(payload_json)
        except Exception:
            payload = {}

        # Extract user prompt
        prompt = str(((payload.get("content") or {}).get("text") or "")).strip()
        if not prompt:
            prompt = DEFAULT_DIAGNOSE_PROMPT
        
        # Emit start event
        _emit_event({
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "agent": "diagnose",
            "status": "diagnose_started",
        })

        # Extract image from payload
        image_bytes, mime_type, source_kind = _resolve_image(payload)
        if not image_bytes:
            _emit_event({
                "request_id": request_id,
                "case_id": case_id,
                "client_id": client_id,
                "message_id": message_id,
                "agent": "diagnose",
                "status": "diagnose_failed",
                "error": "image_unavailable",
            })
            _redis_text.xack(DIAGNOSE_JOB_STREAM, DIAGNOSE_CONSUMER_GROUP, msg_id)
            _redis_text.xdel(DIAGNOSE_JOB_STREAM, msg_id)
            attachments = ((payload.get("content") or {}).get("attachments") or [])
            logger.warning(
                "diagnose job missing image: request_id=%s client_id=%s "
                "attachments=%s telegram_media_ref=%s",
                request_id,
                client_id,
                len(attachments),
                _telegram_media_ref_from_payload(payload),
            )
            return

        logger.info(
            "diagnose dispatch: request_id=%s client_id=%s bytes=%s mime=%s source=%s",
            request_id, client_id, len(image_bytes), mime_type, source_kind,
        )

        metadata_obj = payload.get("metadata") or {}
        extras_obj = metadata_obj.get("extras") if isinstance(metadata_obj, dict) else {}
        extras_obj = extras_obj if isinstance(extras_obj, dict) else {}
        group_id = str(extras_obj.get("group_id") or "").strip()
        group_expected_count = int(str(extras_obj.get("group_expected_count") or "0") or "0")

        result = _post_to_medgemma_base64(
            image_bytes=image_bytes,
            prompt=prompt,
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            message_id=message_id,
            channel=channel,
            group_id=group_id,
            group_expected_count=group_expected_count,
        )
        medgemma_job_id = str(result.get("job_id") or "")
        medgemma_status = str(result.get("status") or "queued")

        # Emit queued event
        _emit_event({
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "agent": "diagnose",
            "status": "diagnose_inference_queued",
            "diagnosis_status": medgemma_status,
            "diagnosis_job_id": medgemma_job_id,
            "mime_type": mime_type,
            "endpoint": result.get("_endpoint", ""),
        })

        _redis_text.xack(DIAGNOSE_JOB_STREAM, DIAGNOSE_CONSUMER_GROUP, msg_id)
        _redis_text.xdel(DIAGNOSE_JOB_STREAM, msg_id)

        # Mark as processed for idempotency
        reclaim_logic.mark_processed(DIAGNOSE_JOB_STREAM, msg_id)

    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        body = ""
        try:
            body = exc.response.text[:500] if exc.response is not None else ""
        except Exception:
            body = ""
        logger.exception(
            "medgemma http error: status=%s request_id=%s body=%s",
            status_code, request_id, body,
        )
        _emit_event({
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "agent": "diagnose",
            "status": "diagnose_failed",
            "error": f"http_{status_code}",
            "detail": body,
        })
        _redis_text.xadd(
            DIAGNOSE_DLQ_STREAM,
            {
                "request_id": request_id,
                "case_id": case_id,
                "client_id": client_id,
                "source_stream": DIAGNOSE_JOB_STREAM,
                "error": f"http_{status_code}",
                "detail": body,
                "payload": payload_json,
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
        _redis_text.xack(DIAGNOSE_JOB_STREAM, DIAGNOSE_CONSUMER_GROUP, msg_id)
        _redis_text.xdel(DIAGNOSE_JOB_STREAM, msg_id)

    except Exception as exc:
        logger.exception(
            "diagnose job failed: request_id=%s client_id=%s",
            request_id, client_id,
        )
        _redis_text.xadd(
            DIAGNOSE_DLQ_STREAM,
            {
                "request_id": request_id,
                "case_id": case_id,
                "client_id": client_id,
                "source_stream": DIAGNOSE_JOB_STREAM,
                "error": str(exc),
                "payload": payload_json,
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
        _emit_event({
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "agent": "diagnose",
            "status": "diagnose_failed",
            "error": str(exc),
        })
        _redis_text.xack(DIAGNOSE_JOB_STREAM, DIAGNOSE_CONSUMER_GROUP, msg_id)
        _redis_text.xdel(DIAGNOSE_JOB_STREAM, msg_id)


def _start_reclaim_thread(
    stop_event: threading.Event,
) -> threading.Thread:
    """
    Start a background thread that periodically reclaims stale pending messages.

    Args:
        stop_event: Cooperative shutdown flag.

    Returns:
        The started reclaim thread.
    """
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:
                def _reclaim_handler(msg_id: str, fields: dict[str, Any]) -> None:
                    # Mark as processed before handling to ensure idempotency
                    if reclaim_logic.mark_processed(DIAGNOSE_JOB_STREAM, msg_id):
                        try:
                            _process_stream_message(msg_id, fields)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                DIAGNOSE_JOB_STREAM, msg_id,
                            )
                            # Mark as not processed so it can be retried
                            _redis_text.delete(reclaim_logic.processed_key(DIAGNOSE_JOB_STREAM, msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=DIAGNOSE_JOB_STREAM,
                    group=DIAGNOSE_CONSUMER_GROUP,
                    consumer=DIAGNOSE_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=DIAGNOSE_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        DIAGNOSE_JOB_STREAM, reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", DIAGNOSE_JOB_STREAM)

            # Wait for the interval or stop event
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"diagnose-reclaim-{DIAGNOSE_JOB_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


def _drain_completed(inflight: set[Future[None]], *, timeout: float) -> None:
    """
    Drain completed futures from the inflight set.
    
    Args:
        inflight: Set of pending futures
        timeout: Timeout for waiting
    """
    if not inflight:
        return
    done, _ = wait(inflight, timeout=timeout, return_when=FIRST_COMPLETED)
    for future in done:
        inflight.remove(future)
        try:
            future.result()
        except Exception:
            logger.exception("unexpected diagnose task failure")


def run_worker_forever() -> None:
    """
    Run the diagnose worker forever.
    """
    check_redis_connectivity(decode_responses=True)
    _ensure_consumer_group()

    logger.info(
        "diagnose worker started: stream=%s group=%s consumer=%s gateway=%s",
        DIAGNOSE_JOB_STREAM,
        DIAGNOSE_CONSUMER_GROUP,
        DIAGNOSE_CONSUMER_NAME,
        MEDGEMMA_GATEWAY_URL,
    )

    stop_event = threading.Event()

    # Start reclaim thread
    reclaim_thread = _start_reclaim_thread(stop_event)

    with ThreadPoolExecutor(max_workers=WORKERS_MAX_INFLIGHT) as pool:
        inflight: set[Future[None]] = set()

        try:
            while not stop_event.is_set():
                _drain_completed(inflight, timeout=0.0)

                available_capacity = WORKERS_MAX_INFLIGHT - len(inflight)
                if available_capacity <= 0:
                    _drain_completed(inflight, timeout=1.0)
                    continue

                read_count = min(WORKERS_STREAM_BATCH_SIZE, available_capacity)
                resp = run_with_redis_retries(
                    lambda: _redis_text.xreadgroup(
                        groupname=DIAGNOSE_CONSUMER_GROUP,
                        consumername=DIAGNOSE_CONSUMER_NAME,
                        streams={DIAGNOSE_JOB_STREAM: ">"},
                        count=read_count,
                        block=WORKERS_STREAM_BLOCK_MS,
                    ),
                    operation_name="diagnose_xreadgroup",
                )
                if not resp:
                    continue

                for _, messages in resp:
                    for msg_id, fields in messages:
                        if len(inflight) >= WORKERS_MAX_INFLIGHT:
                            break
                        inflight.add(pool.submit(_process_stream_message, str(msg_id), fields))
        except KeyboardInterrupt:
            logger.info("diagnose worker received KeyboardInterrupt; shutting down")
        finally:
            stop_event.set()
            reclaim_thread.join(timeout=5.0)
            logger.info("diagnose worker stopped")


if __name__ == "__main__":
    run_worker_forever()

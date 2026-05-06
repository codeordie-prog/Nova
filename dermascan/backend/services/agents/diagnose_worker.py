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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import httpx
import redis
from dotenv import load_dotenv

from dermascan.shared.logger import get_logger

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "")
MEDGEMMA_GATEWAY_URL: str = os.getenv("MEDGEMMA_GATEWAY_URL", "http://127.0.0.1:8080")
MEDGEMMA_AUTH_HEADER: str = os.getenv("MEDGEMMA_AUTH_HEADER", "")
MEDGEMMA_AUTH_VALUE: str = os.getenv("MEDGEMMA_AUTH_VALUE", "")
MEDGEMMA_HTTP_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_HTTP_TIMEOUT_S", "60"))

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

logger: logging.Logger = get_logger("agents")

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is required for the diagnose worker")

_redis_text: redis.Redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
_redis_binary: redis.Redis = redis.Redis.from_url(REDIS_URL, decode_responses=False)


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


def _resolve_image(payload: dict[str, Any]) -> tuple[bytes, str, str]:
    """
    Resolve image bytes from the diagnose payload.

    Returns:
        (image_bytes, mime_type, source_kind). image_bytes is empty when no
        image was found.
    """
    content = payload.get("content") or {}
    attachments = content.get("attachments") or []

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
                return base64.b64decode(value, validate=True), mime_type, kind
            except Exception:
                logger.exception("invalid base64 attachment")
                continue

        if kind == "blob_ref":
            raw = _redis_binary.get(value)
            if raw:
                return raw, mime_type, kind
            logger.warning("media_ref not found or expired: %s", value)
            continue

    return b"", "image/jpeg", ""


def _post_to_medgemma_base64(
    image_bytes: bytes,
    prompt: str,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
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
    response = httpx.post(
        endpoint,
        json={
            "image_base64": image_b64,
            "prompt": prompt,
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": message_id,
            "channel": channel,
        },
        headers=headers or None,
        timeout=MEDGEMMA_HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json() if response.content else {}
    data["_endpoint"] = endpoint
    return data


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
            logger.warning(
                "diagnose job missing image: request_id=%s client_id=%s",
                request_id, client_id,
            )
            return

        logger.info(
            "diagnose dispatch: request_id=%s client_id=%s bytes=%s mime=%s source=%s",
            request_id, client_id, len(image_bytes), mime_type, source_kind,
        )

        result = _post_to_medgemma_base64(
            image_bytes=image_bytes,
            prompt=prompt,
            request_id=request_id,
            case_id=case_id,
            client_id=client_id,
            message_id=message_id,
            channel=channel,
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
    _redis_text.ping()
    _ensure_consumer_group()

    logger.info(
        "diagnose worker started: stream=%s group=%s consumer=%s gateway=%s",
        DIAGNOSE_JOB_STREAM,
        DIAGNOSE_CONSUMER_GROUP,
        DIAGNOSE_CONSUMER_NAME,
        MEDGEMMA_GATEWAY_URL,
    )

    with ThreadPoolExecutor(max_workers=WORKERS_MAX_INFLIGHT) as pool:
        inflight: set[Future[None]] = set()

        while True:
            _drain_completed(inflight, timeout=0.0)

            available_capacity = WORKERS_MAX_INFLIGHT - len(inflight)
            if available_capacity <= 0:
                _drain_completed(inflight, timeout=1.0)
                continue

            read_count = min(WORKERS_STREAM_BATCH_SIZE, available_capacity)
            resp = _redis_text.xreadgroup(
                groupname=DIAGNOSE_CONSUMER_GROUP,
                consumername=DIAGNOSE_CONSUMER_NAME,
                streams={DIAGNOSE_JOB_STREAM: ">"},
                count=read_count,
                block=WORKERS_STREAM_BLOCK_MS,
            )
            if not resp:
                continue

            for _, messages in resp:
                for msg_id, fields in messages:
                    if len(inflight) >= WORKERS_MAX_INFLIGHT:
                        break
                    inflight.add(pool.submit(_process_stream_message, str(msg_id), fields))


if __name__ == "__main__":
    run_worker_forever()

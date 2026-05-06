"""
Author: Dermascan Team
Date: 2026-04-25
Description: Diagnose tools for the diagnose agent.
"""

import base64
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv
from langchain.tools import tool
from redis import Redis

load_dotenv()

REDIS_URL: str = os.getenv("REDIS_URL", "")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
MEDGEMMA_GATEWAY_URL: str = os.getenv("MEDGEMMA_GATEWAY_URL", "http://127.0.0.1:8000")

_redis_text: Redis | None = Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
_redis_binary: Redis | None = Redis.from_url(REDIS_URL, decode_responses=False) if REDIS_URL else None


@tool
def get_time(*args, **kwargs):
    """Useful when you need to know the current time."""
    return datetime.now()


def _resolve_image_base64(image_base64: str, media_ref: str) -> str:
    if image_base64:
        return image_base64

    if media_ref and _redis_binary is not None:
        raw_bytes = _redis_binary.get(media_ref)
        if raw_bytes:
            return base64.b64encode(raw_bytes).decode("ascii")

    return ""


@tool
def send_prompt_and_image_to_medgemma(
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    prompt: str,
    image_base64: str = "",
    media_ref: str = "",
    mime_type: str = "image/jpeg",
) -> dict[str, str]:
    """
    Send diagnosis prompt and image to the MedGemma gateway endpoint.

    Args:
        request_id: Unique request identifier for tracing.
        case_id: Case identifier.
        client_id: End-user/client identifier.
        message_id: Source message identifier.
        prompt: Diagnosis prompt to send to MedGemma.
        image_base64: Optional base64 encoded image content.
        media_ref: Optional Redis media key (for example: media:test:...).
        mime_type: Image MIME type.

    Returns:
        A dict containing status, endpoint, and medgemma job metadata.
    """
    if not prompt.strip():
        return {"status": "error", "detail": "prompt is required"}

    resolved_b64 = _resolve_image_base64(image_base64=image_base64, media_ref=media_ref)
    if not resolved_b64:
        return {
            "status": "error",
            "detail": "image is required (provide image_base64 or valid media_ref)",
        }

    endpoint = f"{MEDGEMMA_GATEWAY_URL.rstrip('/')}/diagnose_base64"
    try:
        response = httpx.post(
            endpoint,
            json={"image_base64": resolved_b64, "prompt": prompt},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
        medgemma_job_id = str(payload.get("job_id") or "")
        medgemma_status = str(payload.get("status") or "queued")

        if _redis_text is not None:
            _redis_text.xadd(
                EVENTS_STREAM,
                {
                    "request_id": request_id,
                    "case_id": case_id,
                    "client_id": client_id,
                    "message_id": message_id,
                    "agent": "diagnose",
                    "status": "diagnose_inference_queued",
                    "medgemma_status": medgemma_status,
                    "medgemma_job_id": medgemma_job_id,
                    "mime_type": mime_type,
                },
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )

        return {
            "status": "queued",
            "endpoint": endpoint,
            "medgemma_status": medgemma_status,
            "medgemma_job_id": medgemma_job_id,
            "request_id": request_id,
        }
    except Exception as exc:
        if _redis_text is not None:
            _redis_text.xadd(
                EVENTS_STREAM,
                {
                    "request_id": request_id,
                    "case_id": case_id,
                    "client_id": client_id,
                    "message_id": message_id,
                    "agent": "diagnose",
                    "status": "diagnose_failed",
                    "error": str(exc),
                },
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            )

        return {
            "status": "error",
            "endpoint": endpoint,
            "detail": str(exc),
            "request_id": request_id,
        }


diagnose_tools: list = [get_time, send_prompt_and_image_to_medgemma]

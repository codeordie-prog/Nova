"""
Author: DermascanTeam
Date: 2026-04-20
Description: Orchestrator tools for the orchestrator Agent

"""

import os
import json
import time
import hashlib
from logging import Logger
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import load_dotenv
from typing import Any

import httpx
from langchain.tools import tool
from redis import Redis
from tavily import TavilyClient

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

load_dotenv()

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY_QUTIS", "")

# Initialize Logger
logger: Logger = get_logger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
MEDGEMMA_GATEWAY_URL: str = os.getenv("MEDGEMMA_GATEWAY_URL", "http://127.0.0.1:8080")
MEDGEMMA_ENDPOINT_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_ENDPOINT_TIMEOUT_S", "5"))
MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S: float = float(os.getenv("MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S", "2"))
MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS: int = max(0, int(os.getenv("MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS", "1")))
MEDGEMMA_ENDPOINT_RETRY_DELAY_S: float = max(0.0, float(os.getenv("MEDGEMMA_ENDPOINT_RETRY_DELAY_S", "0.2")))

CASE_STATUS_SCAN_PAGE: int = max(1, int(os.getenv("CASE_STATUS_SCAN_PAGE", "500")))
CASE_STATUS_SCAN_MAX: int = max(CASE_STATUS_SCAN_PAGE, int(os.getenv("CASE_STATUS_SCAN_MAX", "5000")))
CASE_STATUS_HISTORY: int = max(1, int(os.getenv("CASE_STATUS_HISTORY", "5")))
SEARCH_RESULT_CONTENT_CHARS: int = max(200, int(os.getenv("SEARCH_RESULT_CONTENT_CHARS", "700")))
SEARCH_RESULT_TOTAL_CHARS: int = max(1000, int(os.getenv("SEARCH_RESULT_TOTAL_CHARS", "4500")))
TAVILY_TIMEOUT_S: float = max(1.0, float(os.getenv("TAVILY_TIMEOUT_S", "12")))
SEARCH_CACHE_TTL_S: int = max(0, int(os.getenv("SEARCH_CACHE_TTL_S", str(60 * 60 * 6))))
MEDGEMMA_HEALTH_TIMEOUT_S: float = max(0.5, float(os.getenv("MEDGEMMA_HEALTH_TIMEOUT_S", "3")))


def _init_redis_client() -> Redis | None:
    """Build a resilient shared Redis client, failing open when unavailable."""
    if not REDIS_URL:
        return None
    try:
        return get_redis_client(decode_responses=True)
    except Exception:
        logger.exception("failed to initialize resilient redis client")
        return None


_redis: Redis | None = _init_redis_client()
_SOUL_MD_CACHE: str | None = None
_AUDIT_ENQUEUED_KEY_PREFIX: str = "diagnose:enqueued"
_AUDIT_ENQUEUED_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_INGRESS_PAYLOAD_CACHE_PREFIX: str = "orchestrator:ingress_payload"
_INGRESS_PAYLOAD_CACHE_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_LATEST_IMAGE_REQUEST_BY_CLIENT_PREFIX: str = "orchestrator:latest_image_request:client"
_LATEST_IMAGE_REQUEST_BY_CASE_PREFIX: str = "orchestrator:latest_image_request:case"
_LATEST_IMAGE_REQUEST_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_SEARCH_CACHE_PREFIX: str = "orchestrator:search_cache"



def payload_has_image_attachment(payload: dict) -> bool:
    """True when ingress payload includes at least one image attachment."""
    attachments = (payload.get("content") or {}).get("attachments") or []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = str(attachment.get("type") or "")
        mime_type = str(attachment.get("mime_type") or "")
        if attachment_type != "image" and not mime_type.startswith("image/"):
            continue
        source = attachment.get("source") or {}
        if str(source.get("value") or "").strip():
            return True
    return False


def _payload_cache_key(request_id: str) -> str:
    """Build Redis key for request-scoped ingress payload cache."""
    return f"{_INGRESS_PAYLOAD_CACHE_PREFIX}:{request_id}"


def _latest_image_request_key_for_client(client_id: str) -> str:
    """Build Redis key for latest image request id scoped by client."""
    return f"{_LATEST_IMAGE_REQUEST_BY_CLIENT_PREFIX}:{client_id}"


def _latest_image_request_key_for_case(case_id: str) -> str:
    """Build Redis key for latest image request id scoped by case."""
    return f"{_LATEST_IMAGE_REQUEST_BY_CASE_PREFIX}:{case_id}"


def cache_ingress_payload(
    *,
    request_id: str,
    payload: str,
    case_id: str = "",
    client_id: str = "",
    message_id: str = "",
    channel: str = "",
) -> None:
    """
    Cache the ingress envelope by request id for deterministic tool calls.

    Args:
        request_id: Request identifier.
        payload: Canonical payload JSON string.
        case_id: Case identifier.
        client_id: End-user/client identifier.
        message_id: Source message identifier.
        channel: Source channel name.
    """
    if _redis is None:
        return
    rid = str(request_id or "").strip()
    if not rid:
        return
    try:
        cached_envelope = {
            "request_id": rid,
            "case_id": str(case_id or rid),
            "client_id": str(client_id or "anonymous"),
            "message_id": str(message_id or rid),
            "channel": str(channel or "whatsapp"),
            "payload": str(payload or "{}"),
        }
        _redis.setex(
            _payload_cache_key(rid),
            _INGRESS_PAYLOAD_CACHE_TTL_SEC,
            json.dumps(cached_envelope, default=str),
        )
    except Exception:
        logger.exception("failed to cache ingress envelope: request_id=%s", rid)


def _get_cached_ingress_payload(request_id: str) -> str:
    """
    Get cached ingress payload for request id.

    Returns:
        Cached payload JSON string or empty string.
    """
    if _redis is None:
        return ""
    rid = str(request_id or "").strip()
    if not rid:
        return ""
    try:
        return str(_redis.get(_payload_cache_key(rid)) or "")
    except Exception:
        logger.exception("failed to read ingress payload cache: request_id=%s", rid)
        return ""


def _stringify_cached_payload(payload: Any) -> str:
    """
    Convert a cached payload value into canonical JSON text.

    Args:
        payload: Cached payload as a JSON string or mapping.

    Returns:
        JSON string payload, or `{}` when no payload is available.
    """
    if isinstance(payload, str):
        return payload or "{}"
    if isinstance(payload, dict):
        return json.dumps(payload, default=str)
    return "{}"


def _get_cached_ingress_envelope(request_id: str) -> dict[str, str]:
    """
    Read cached ingress envelope fields needed for audit dispatch.

    Args:
        request_id: Request id from the current orchestrator prompt.

    Returns:
        Dict containing request_id, case_id, client_id, message_id, channel,
        and payload. Empty dict when the cache is unavailable.
    """
    rid = str(request_id or "").strip()
    cached = _get_cached_ingress_payload(rid)
    if not cached:
        return {}

    try:
        cached_obj: Any = json.loads(cached)
    except Exception:
        logger.exception("cached ingress envelope malformed: request_id=%s", rid)
        return {}

    if not isinstance(cached_obj, dict):
        return {}

    if "payload" in cached_obj:
        return {
            "request_id": str(cached_obj.get("request_id") or rid),
            "case_id": str(cached_obj.get("case_id") or rid),
            "client_id": str(cached_obj.get("client_id") or "anonymous"),
            "message_id": str(cached_obj.get("message_id") or rid),
            "channel": str(cached_obj.get("channel") or "whatsapp"),
            "payload": _stringify_cached_payload(cached_obj.get("payload")),
        }

    # Backward compatibility for old cache entries that stored only the payload.
    return {
        "request_id": rid,
        "case_id": rid,
        "client_id": "anonymous",
        "message_id": rid,
        "channel": "whatsapp",
        "payload": json.dumps(cached_obj, default=str),
    }


def _resolve_canonical_payload(*, request_id: str, payload: str) -> str:
    """
    Ensure payload is valid JSON; recover from cache when malformed.

    Args:
        request_id: Request id used for cache fallback.
        payload: Payload provided by tool call.

    Returns:
        Valid payload JSON string.
    """
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return json.dumps(parsed, default=str)
    except Exception:
        logger.warning("tool payload json invalid; attempting cache fallback: request_id=%s", request_id)

    envelope = _get_cached_ingress_envelope(request_id)
    if envelope:
        try:
            parsed = json.loads(envelope["payload"])
            if isinstance(parsed, dict):
                return json.dumps(parsed, default=str)
        except Exception:
            logger.exception("cached ingress payload malformed: request_id=%s", request_id)
    return payload


def _merge_collected_context_into_payload(payload: str, collected_context: str) -> str:
    """
    Merge orchestrator-collected context into payload.content.text for audit.

    Args:
        payload: Canonical payload JSON string.
        collected_context: Structured intake summary prepared by orchestrator.

    Returns:
        Updated payload JSON string. Returns original payload on parse failures.
    """
    context_text = str(collected_context or "").strip()
    if not context_text:
        return payload

    try:
        payload_obj: dict[str, Any] = json.loads(payload)
    except Exception:
        logger.exception("failed to parse payload while merging collected_context")
        return payload

    content_obj = payload_obj.get("content")
    if not isinstance(content_obj, dict):
        content_obj = {}
        payload_obj["content"] = content_obj
    existing_text = str(content_obj.get("text") or "").strip()
    content_obj["text"] = (
        f"{existing_text}\n\nCollected context:\n{context_text}".strip()
        if existing_text
        else f"Collected context:\n{context_text}"
    )

    metadata_obj = payload_obj.get("metadata")
    if not isinstance(metadata_obj, dict):
        metadata_obj = {}
        payload_obj["metadata"] = metadata_obj
    extras_obj = metadata_obj.get("extras")
    if not isinstance(extras_obj, dict):
        extras_obj = {}
        metadata_obj["extras"] = extras_obj
    extras_obj["audit_context_enriched"] = "true"
    # Backward compatibility for any downstream readers that still inspect this marker.
    extras_obj["diagnose_prompt_enriched"] = "true"

    return json.dumps(payload_obj, default=str)


def _resolve_image_url_from_payload(payload_obj: dict[str, Any]) -> str:
    """
    Resolve a downloadable image URL from canonical payload attachments.

    Args:
        payload_obj: Canonical payload object.

    Returns:
        Image URL string, or empty string when not resolvable.
    """
    attachments = (payload_obj.get("content") or {}).get("attachments") or []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = str(attachment.get("type") or "")
        mime_type = str(attachment.get("mime_type") or "")
        if attachment_type != "image" and not mime_type.startswith("image/"):
            continue

        source = attachment.get("source") or {}
        source_kind = str(source.get("kind") or "").strip().lower()
        source_value = str(source.get("value") or "").strip()
        if not source_value:
            continue

        if source_kind == "url" and source_value.lower().startswith(("http://", "https://")):
            return source_value

        # Blob refs are expected to map to signed/object URLs in Redis.
        if source_kind == "blob_ref" and _redis is not None:
            try:
                cached = str(_redis.get(source_value) or "").strip()
                if cached.lower().startswith(("http://", "https://")):
                    return cached
            except Exception:
                logger.exception("failed to resolve blob_ref to image url: ref=%s", source_value)

    # Fallback for Telegram-style metadata extras.
    extras_obj = ((payload_obj.get("metadata") or {}).get("extras") or {})
    if isinstance(extras_obj, dict):
        telegram_media_ref = str(extras_obj.get("telegram_media_ref") or "").strip()
        if telegram_media_ref and _redis is not None:
            try:
                cached = str(_redis.get(telegram_media_ref) or "").strip()
                if cached.lower().startswith(("http://", "https://")):
                    return cached
            except Exception:
                logger.exception(
                    "failed to resolve telegram_media_ref to image url: ref=%s",
                    telegram_media_ref,
                )

    return ""


def _extract_prompt_context(payload_obj: dict[str, Any]) -> str:
    """
    Build the context string sent to MedGemma from canonical payload content.

    Args:
        payload_obj: Canonical payload object.

    Returns:
        Context text (possibly empty).
    """
    content_obj = payload_obj.get("content") or {}
    if not isinstance(content_obj, dict):
        return ""
    return str(content_obj.get("text") or "").strip()


def _emit_orchestrator_event(fields: dict[str, str]) -> None:
    """
    Best-effort append to orchestrator events stream.

    Args:
        fields: Event fields to append.
    """
    if _redis is None:
        return
    try:
        _redis.xadd(
            EVENTS_STREAM,
            {k: ("" if v is None else str(v)) for k, v in fields.items()},
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("failed to append orchestrator event: status=%s", fields.get("status"))


def _parse_retry_after_seconds(value: str) -> str:
    """
    Parse Retry-After header into a user-friendly numeric-seconds string.

    Args:
        value: Raw Retry-After header value.

    Returns:
        Parsed integer seconds as string, or empty string when parsing fails.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        seconds = max(1, int(float(raw)))
        return str(seconds)
    except (TypeError, ValueError):
        return ""


def _dispatch_audit_image_ref(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    image_url: str,
    context: str,
) -> dict[str, str]:
    """
    Dispatch directly to MedGemma ref endpoint.

    Args:
        request_id: Request identifier.
        case_id: Case identifier.
        client_id: Client identifier.
        message_id: Message identifier.
        channel: Source channel.
        image_url: Signed/downloadable image URL.
        context: Model context text.

    Returns:
        Status dictionary for tool caller.
    """
    endpoint = f"{MEDGEMMA_GATEWAY_URL.rstrip('/')}/audit_image_ref"
    payload = {
        "request_id": request_id,
        "case_id": case_id,
        "client_id": client_id,
        "message_id": message_id,
        "channel": channel,
        "image_url": image_url,
        "context": context,
    }

    timeout = httpx.Timeout(
        timeout=MEDGEMMA_ENDPOINT_TIMEOUT_S,
        connect=MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S,
    )
    response: httpx.Response | None = None

    for attempt in range(MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS + 1):
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                timeout=timeout,
            )
            break
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError):
            if attempt >= MEDGEMMA_ENDPOINT_RETRY_ATTEMPTS:
                raise
            time.sleep(MEDGEMMA_ENDPOINT_RETRY_DELAY_S * (attempt + 1))

    if response is None:
        raise RuntimeError("audit dispatch produced no HTTP response")

    if response.status_code == 429:
        retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After", ""))
        return {
            "status": "under_pressure",
            "request_id": request_id,
            "endpoint": endpoint,
            "retry_after_seconds": retry_after or "1",
            "detail": "MedGemma queue is saturated; retry later.",
            "instruction": (
                "MedGemma is currently under pressure. Do NOT call this tool again right now. "
                "Tell the user their request was received but processing is delayed."
            ),
        }

    response.raise_for_status()
    data: dict[str, Any] = response.json() if response.content else {}
    return {
        "status": str(data.get("status") or "queued"),
        "request_id": request_id,
        "endpoint": endpoint,
        "medgemma_job_id": str(data.get("job_id") or ""),
    }


@tool
def get_time() -> str:
    """
    Useful when you need to know the current time
    """
    logger.info(f"TOOL CALL: get_time")
    return datetime.now(timezone.utc).isoformat()


@tool
def save_latest_image_request_id(request_id: str) -> dict[str, str]:
    """
    Save the latest image-bearing request_id for this conversation context.

    Args:
        request_id: The request id that contains the uploaded image.

    Returns:
        Status payload indicating save result and scope metadata.
    """
    logger.info("TOOL CALL: save_latest_image_request_id: request_id=%s", request_id)

    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured", "request_id": request_id}

    envelope = _get_cached_ingress_envelope(request_id)
    if not envelope:
        return {
            "status": "error",
            "detail": "ingress cache not found for request_id",
            "request_id": request_id,
        }

    try:
        payload_obj = json.loads(str(envelope.get("payload") or "{}"))
    except Exception:
        payload_obj = {}

    if not payload_has_image_attachment(payload_obj):
        return {
            "status": "blocked_missing_image",
            "request_id": str(envelope.get("request_id") or request_id),
            "instruction": (
                "This request does not contain an image attachment. Do NOT call this tool again for this request_id. "
                "Ask the user to upload a clear image first."
            ),
        }

    resolved_request_id = str(envelope.get("request_id") or request_id).strip()
    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or resolved_request_id).strip()

    try:
        _redis.setex(
            _latest_image_request_key_for_client(client_id),
            _LATEST_IMAGE_REQUEST_TTL_SEC,
            resolved_request_id,
        )
        _redis.setex(
            _latest_image_request_key_for_case(case_id),
            _LATEST_IMAGE_REQUEST_TTL_SEC,
            resolved_request_id,
        )
    except Exception:
        logger.exception(
            "failed to save latest image request id: request_id=%s client_id=%s case_id=%s",
            resolved_request_id,
            client_id,
            case_id,
        )
        return {
            "status": "error",
            "detail": "failed to persist latest image request id",
            "request_id": resolved_request_id,
        }

    return {
        "status": "saved",
        "request_id": resolved_request_id,
        "client_id": client_id,
        "case_id": case_id,
        "instruction": (
            "Latest image request_id has been saved. Continue collecting missing intake context. "
            "Before routing, call get_latest_image_request_id to retrieve the request_id to enqueue."
        ),
    }


@tool
def get_latest_image_request_id(request_id: str) -> dict[str, str]:
    """
    Get the latest saved image-bearing request_id for this conversation context.

    Args:
        request_id: Current turn request id used to resolve client/case scope.

    Returns:
        Status payload with the latest image request id when available.
    """
    logger.info("TOOL CALL: get_latest_image_request_id: request_id=%s", request_id)

    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured", "request_id": request_id}

    envelope = _get_cached_ingress_envelope(request_id)
    if not envelope:
        return {
            "status": "error",
            "detail": "ingress cache not found for request_id",
            "request_id": request_id,
        }

    client_id = str(envelope.get("client_id") or "anonymous").strip()
    case_id = str(envelope.get("case_id") or "").strip()

    latest_request_id = ""
    try:
        if case_id:
            latest_request_id = str(_redis.get(_latest_image_request_key_for_case(case_id)) or "").strip()
        if not latest_request_id and client_id:
            latest_request_id = str(_redis.get(_latest_image_request_key_for_client(client_id)) or "").strip()
    except Exception:
        logger.exception(
            "failed to read latest image request id: request_id=%s client_id=%s case_id=%s",
            request_id,
            client_id,
            case_id,
        )
        return {
            "status": "error",
            "detail": "failed to read latest image request id",
            "request_id": request_id,
        }

    if not latest_request_id:
        return {
            "status": "not_found",
            "request_id": request_id,
            "instruction": (
                "No saved image request_id found yet for this conversation. "
                "If the user shared an image in this intake flow, call save_latest_image_request_id with that image request_id first."
            ),
        }

    return {
        "status": "found",
        "request_id": request_id,
        "latest_image_request_id": latest_request_id,
        "instruction": (
            "Use latest_image_request_id as the request_id argument for enque_request_for_auditing_stream when routing criteria are met."
        ),
    }


@tool
def enque_request_for_auditing_stream(request_id: str, collected_context: str = "") -> dict[str, str]:
    """
    Route the current request to the specialist team for image audit.

    Args:
        request_id: The exact request_id shown in the current orchestrator input.
        collected_context: A concise structured summary for the image audit. Include
            only facts gathered from the user, not raw chat or internal details.

    Returns:
        Dispatch result with status and job metadata from the image audit worker.
    """

    logger.info(
        "TOOL CALL: enque_request_for_auditing_stream: request_id=%s, collected_context=%s",
        request_id,
        collected_context,
    )

    envelope = _get_cached_ingress_envelope(request_id)
    if not envelope:
        return {
            "status": "error",
            "detail": "ingress cache not found for request_id",
            "request_id": request_id,
        }

    canonical_payload = _resolve_canonical_payload(
        request_id=envelope["request_id"],
        payload=envelope["payload"],
    )

    # Hard invariant: image audit requires an image. Block (terminally) when none
    # is present so the agent cannot loop on this tool for text-only turns.
    try:
        canonical_obj: dict[str, Any] = json.loads(canonical_payload)
    except Exception:
        canonical_obj = {}
    if not payload_has_image_attachment(canonical_obj):
        logger.info("audit tool blocked (no image): request_id=%s", envelope["request_id"])
        return {
            "status": "blocked_missing_image",
            "request_id": envelope["request_id"],
            "instruction": (
                "No image is attached to this request. Do NOT call this tool again. "
                "Ask the user to upload a clear photo of the affected skin area, then "
                "reply to the user directly."
            ),
        }

    merged_payload = _merge_collected_context_into_payload(canonical_payload, collected_context)
    logger.debug(
        "audit tool merged payload prepared: request_id=%s bytes=%s",
        envelope["request_id"],
        len(merged_payload),
    )

    try:
        merged_obj: dict[str, Any] = json.loads(merged_payload)
    except Exception:
        merged_obj = canonical_obj

    image_url = _resolve_image_url_from_payload(merged_obj)
    if not image_url:
        logger.info(
            "audit tool blocked (image url unresolved): request_id=%s",
            envelope["request_id"],
        )
        return {
            "status": "blocked_missing_image",
            "request_id": envelope["request_id"],
            "instruction": (
                "An image exists but no downloadable image URL could be resolved for this request. "
                "Do NOT call this tool again. Ask the user to upload the image again, then reply directly."
            ),
        }

    context_text = _extract_prompt_context(merged_obj)
    if not context_text:
        context_text = "Provide a structured dermatological or Product review assessment of the provided image."

    dedupe_key = f"{_AUDIT_ENQUEUED_KEY_PREFIX}:{envelope['request_id']}"
    if _redis is not None and not _redis.set(dedupe_key, "1", nx=True, ex=_AUDIT_ENQUEUED_TTL_SEC):
        logger.info("audit request already dispatched: request_id=%s", envelope["request_id"])
        return {
            "status": "already_queued",
            "request_id": envelope["request_id"],
            "instruction": (
                "This image audit request was already dispatched. Do NOT call this tool again. "
                "Tell the user their photo is already being reviewed."
            ),
        }

    try:
        result = _dispatch_audit_image_ref(
            request_id=envelope["request_id"],
            case_id=envelope["case_id"],
            client_id=envelope["client_id"],
            message_id=envelope["message_id"],
            channel=envelope["channel"],
            image_url=image_url,
            context=context_text,
        )
    except httpx.HTTPStatusError as exc:
        if _redis is not None:
            _redis.delete(dedupe_key)
        status_code = exc.response.status_code if exc.response is not None else 0
        return {
            "status": "error",
            "request_id": envelope["request_id"],
            "detail": f"audit dispatch failed with HTTP {status_code}",
        }
    except httpx.HTTPError as exc:
        if _redis is not None:
            _redis.delete(dedupe_key)
        return {
            "status": "error",
            "request_id": envelope["request_id"],
            "detail": f"audit dispatch failed: {exc}",
        }

    # Backpressure is not a successful enqueue. Clear the dedupe guard so the
    # request can be retried later, and do not emit a routed event.
    if result.get("status") == "under_pressure":
        if _redis is not None:
            try:
                _redis.delete(dedupe_key)
            except Exception:
                logger.exception(
                    "failed to clear dedupe key after backpressure: request_id=%s",
                    envelope["request_id"],
                )
        return result

    _emit_orchestrator_event(
        {
            "request_id": envelope["request_id"],
            "case_id": envelope["case_id"],
            "client_id": envelope["client_id"],
            "message_id": envelope["message_id"],
            "channel": envelope["channel"],
            "agent": "orchestrator",
            "status": "routed_for_image_auditing",
            "target_endpoint": "/audit_image_ref",
            "medgemma_job_id": result.get("medgemma_job_id", ""),
            "enqueue_source": "orchestrator_tool",
        }
    )

    # Make every terminal outcome explicit so the model stops calling the tool
    # and proceeds to compose the final user-facing reply.
    if result.get("status") in {"queued", "already_queued"}:
        result["instruction"] = (
            "The image audit request is now in the queue. Do NOT call this tool again. "
            "Tell the user their photo was received and is being reviewed by the specialist team, "
            "and that results will follow shortly."
        )
    return result


def _event_ts_ms(event_id: str) -> int:
    """Extract the millisecond timestamp from a Redis stream id of form `ms-seq`."""
    try:
        return int(str(event_id).split("-", 1)[0])
    except Exception:
        return 0


def _event_matches_case(event: dict, case_id: str, request_id: str) -> bool:
    """True when an event belongs to the requested case, by case_id or request_id."""
    if case_id and str(event.get("case_id") or "") == case_id:
        return True
    if request_id and str(event.get("request_id") or "") == request_id:
        return True
    return False


@tool
def read_status_of_a_case(case_id: str, request_id: str = "") -> dict:
    """
    Read the current real-time status of a case from the events stream.

    Scans the events stream from newest to oldest in bounded pages and returns
    the latest event for the case plus a short recent history. Matching is by
    `case_id`, falling back to `request_id` when provided, because some events
    (e.g. downstream errors) may only carry one of the two.

    Args:
        case_id: Case identifier to read status for. Required.
        request_id: Optional request identifier used as a fallback matcher.

    Returns:
        A dict with:
            - `status`: `found`, `not_found`, or `error`
            - `case_id`, `request_id`
            - `latest`: dict of the most recent matching event (agent, event_status,
              message_id, channel, medgemma_status, medgemma_job_id, error,
              event_id, updated_at_ms) when found
            - `history`: list of up to CASE_STATUS_HISTORY recent matching events,
              newest first
            - `scanned`: number of events scanned
    """
    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured"}

    if not case_id and not request_id:
        return {"status": "error", "detail": "case_id or request_id is required"}

    matches: list[dict] = []
    scanned = 0
    cursor = "+"
    logger.info(f"TOOL CALL: read_status_of_a_case: case_id={case_id}, request_id={request_id}")

    try:
        while scanned < CASE_STATUS_SCAN_MAX:
            page = _redis.xrevrange(
                EVENTS_STREAM,
                max=cursor,
                min="-",
                count=CASE_STATUS_SCAN_PAGE,
            )
            if not page:
                break

            for event_id, event_data in page:
                scanned += 1
                if _event_matches_case(event_data, case_id, request_id):
                    matches.append({
                        "event_id": str(event_id),
                        "updated_at_ms": _event_ts_ms(event_id),
                        "agent": event_data.get("agent", "unknown"),
                        "event_status": event_data.get("status", "unknown"),
                        "case_id": event_data.get("case_id", ""),
                        "request_id": event_data.get("request_id", ""),
                        "message_id": event_data.get("message_id", ""),
                        "channel": event_data.get("channel", ""),
                        "audit_status": event_data.get("medgemma_status", ""),
                        "audit_job_id": event_data.get("medgemma_job_id", ""),
                        # Backward compatibility keys
                        "diagnose_status": event_data.get("medgemma_status", ""),
                        "diagnose_job_id": event_data.get("medgemma_job_id", ""),
                        "error": event_data.get("error", ""),
                    })
                    if len(matches) >= CASE_STATUS_HISTORY:
                        break

            if len(matches) >= CASE_STATUS_HISTORY:
                break

            # Advance cursor to just before the oldest id on this page.
            last_id = str(page[-1][0])
            ms, _, seq = last_id.partition("-")
            try:
                seq_int = int(seq or "0")
            except ValueError:
                seq_int = 0
            if seq_int > 0:
                cursor = f"{ms}-{seq_int - 1}"
            else:
                ms_int = int(ms or "0")
                if ms_int <= 0:
                    break
                cursor = f"{ms_int - 1}-18446744073709551615"
    except Exception as exc:
        return {
            "status": "error",
            "detail": f"events stream scan failed: {exc}",
            "case_id": case_id,
            "request_id": request_id,
            "scanned": scanned,
        }

    if not matches:
        return {
            "status": "not_found",
            "case_id": case_id,
            "request_id": request_id,
            "scanned": scanned,
            "instruction": (
                "No status events exist for this case yet. Do NOT call this tool again in this turn. "
                "Tell the user their request is still being processed and results will follow shortly."
            ),
        }

    return {
        "status": "found",
        "case_id": case_id,
        "request_id": request_id,
        "latest": matches[0],
        "history": matches,
        "scanned": scanned,
        "instruction": (
            "This is the current case status. Do NOT call this tool again in this turn. "
            "Use the latest status to compose your reply to the user."
        ),
    }



@tool
def read_soul_md() -> str:
    """
    Read the soul.md file.
    Use this tool when you need to understand your inner self, the role you play beyond the immediate
    role of dermatological diagnosis. Its helpful when you need to engage with your innermost values as part of the large goal
    of serving humanity.
    
    The Soul.md file has contents such as:
        - Core Identity
        - Diagnostic Authority
        - Your Personality
        - Your Origin
    Returns:
        The content of the soul.md file
    """
    global _SOUL_MD_CACHE
    logger.info(f"TOOL CALL: read_soul_md")

    if _SOUL_MD_CACHE is None:
        path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "agents", "soul.md")
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                _SOUL_MD_CACHE = f.read()
        except OSError as exc:
            logger.exception("failed to read soul.md: path=%s", path)
            return (
                f"Soul context is temporarily unavailable ({exc}). "
                "Do NOT call this tool again in this turn. Proceed to respond to the user "
                "using your established identity and values."
            )

    # Append a terminal directive so the agent loads identity context once and
    # proceeds, rather than re-calling this static tool and exhausting the
    # recursion budget.
    return (
        f"{_SOUL_MD_CACHE}\n\n"
        "[SYSTEM NOTE: Your soul/identity context is now loaded. Do NOT call read_soul_md "
        "again in this turn. Proceed to respond to the user.]"
    )




def _get_tavily_client() -> TavilyClient | None:
    if not TAVILY_API_KEY:
        return None
    return TavilyClient(api_key=TAVILY_API_KEY)


def _format_tavily_results(result: dict[str, Any], *, max_results: int) -> str:
    """
    Convert Tavily results into compact JSON safe for an LLM context window.

    Args:
        result: Raw Tavily response.
        max_results: Maximum number of result entries to include.

    Returns:
        Compact JSON containing only title, URL, and a bounded content snippet.
    """
    raw_results = result.get("results") or []
    summaries: list[dict[str, str]] = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("raw_content") or "").strip()
        summaries.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "content": content[:SEARCH_RESULT_CONTENT_CHARS],
            }
        )

    payload = {
        "query": str(result.get("query") or ""),
        "results": summaries,
    }
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(text) <= SEARCH_RESULT_TOTAL_CHARS:
        return text
    return text[:SEARCH_RESULT_TOTAL_CHARS] + "...<truncated>"


def _search_cache_key(
    *, label: str, query: str, include_domains: list[str], max_results: int, search_depth: str
) -> str:
    """Build a deterministic Redis cache key for a search invocation."""
    raw = json.dumps(
        {
            "l": label,
            "q": query.strip().lower(),
            "d": sorted(include_domains),
            "n": max_results,
            "s": search_depth,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{_SEARCH_CACHE_PREFIX}:{digest}"


def _run_tavily_search(
    *,
    label: str,
    query: str,
    max_results: int,
    include_domains: list[str],
    search_depth: str = "",
) -> str:
    """
    Execute a Tavily search with caching, a bounded timeout, and a terminal
    failure directive so the agent degrades gracefully instead of looping.

    Args:
        label: Human-readable tool label used in logs and cache keys.
        query: Search query string.
        max_results: Already-capped number of results to retrieve.
        include_domains: Domain allowlist for the search.
        search_depth: Optional Tavily search depth (e.g. "advanced").

    Returns:
        Compact JSON results string, a not-configured notice, or a terminal
        failure directive string.
    """
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    logger.info("TOOL CALL: %s search: query=%s, max_results=%s", label, query, max_results)

    cache_key = _search_cache_key(
        label=label,
        query=query,
        include_domains=include_domains,
        max_results=max_results,
        search_depth=search_depth,
    )
    if _redis is not None and SEARCH_CACHE_TTL_S > 0:
        try:
            cached = _redis.get(cache_key)
            if cached:
                logger.info("TOOL CALL CACHE HIT: %s search: query=%s", label, query)
                return str(cached)
        except Exception:
            logger.exception("failed to read search cache: key=%s", cache_key)

    search_kwargs: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "include_domains": include_domains,
        "timeout": TAVILY_TIMEOUT_S,
    }
    if search_depth:
        search_kwargs["search_depth"] = search_depth

    try:
        result = client.search(**search_kwargs)
        formatted = _format_tavily_results(result, max_results=max_results)
    except Exception as exc:
        logger.warning("%s search failed: query=%s err=%s", label, query, exc)
        return (
            f"{label} search failed ({exc}). Do NOT call this tool again in this turn. "
            "Answer the user from your existing knowledge and clearly note that live "
            "research was unavailable."
        )

    if _redis is not None and SEARCH_CACHE_TTL_S > 0 and formatted:
        try:
            _redis.setex(cache_key, SEARCH_CACHE_TTL_S, formatted)
        except Exception:
            logger.exception("failed to write search cache: key=%s", cache_key)

    logger.info(
        "TOOL CALL SUCCESS: %s search: query=%s result_chars=%s",
        label,
        query,
        len(formatted),
    )
    return formatted


@tool
def wikipedia_search(query: str, max_results: int = 3) -> str:
    """
    Search Wikipedia content through Tavily.

    Use this tool when you need background explanations, definitions, and
    high-level context. Do not rely on this tool alone for critical medical
    safety claims; cross-check key claims with broader web sources.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
    """
    return _run_tavily_search(
        label="Wikipedia",
        query=query,
        max_results=max(1, min(max_results, 5)),
        include_domains=["wikipedia.org"],
    )


@tool
def internet_search(query: str, max_results: int = 3) -> str:
    """
    Perform general web research through Tavily.

    Use this as the primary research tool for up-to-date and credible external
    sources (guidelines, journals, hospital/public-health pages, and trusted
    references). Prefer this tool for safety checks and practical guidance.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
    """
    return _run_tavily_search(
        label="Internet",
        query=query,
        max_results=max(1, min(max_results, 3)),
        include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "mydawa.com"],
        search_depth="advanced",
    )


@tool
def check_medgemma_health() -> dict[str, str]:
    """
    Check whether the image-audit (MedGemma) service is reachable before routing.

    Use this BEFORE enqueueing an image audit when you are unsure the analysis
    service is available, or after a prior dispatch reported pressure. It performs
    a lightweight, non-mutating reachability probe and never enqueues anything.

    Returns:
        A dict with:
            - `status`: `healthy`, `degraded`, or `unavailable`
            - `endpoint`: the probed gateway URL
            - `retry_after_seconds`: present when the service is rate-limited
            - `instruction`: terminal guidance on whether to enqueue now
    """
    logger.info("TOOL CALL: check_medgemma_health")

    base = MEDGEMMA_GATEWAY_URL.rstrip("/")
    # `/jobs/{id}` returns 404 for unknown ids; any HTTP response proves the
    # gateway is reachable without creating or mutating a job.
    endpoint = f"{base}/jobs/__health_probe__"
    timeout = httpx.Timeout(
        timeout=MEDGEMMA_HEALTH_TIMEOUT_S,
        connect=MEDGEMMA_ENDPOINT_CONNECT_TIMEOUT_S,
    )

    try:
        response = httpx.get(endpoint, timeout=timeout)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        return {
            "status": "unavailable",
            "endpoint": endpoint,
            "detail": f"health probe failed: {exc}",
            "instruction": (
                "The image-audit service is currently unreachable. Do NOT enqueue an image audit now. "
                "Tell the user the analysis service is temporarily unavailable and to try again shortly."
            ),
        }

    if response.status_code == 429:
        retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After", ""))
        return {
            "status": "degraded",
            "endpoint": endpoint,
            "retry_after_seconds": retry_after or "1",
            "instruction": (
                "The image-audit service is under heavy load. Avoid enqueueing right now and "
                "tell the user that processing may be delayed."
            ),
        }

    if response.status_code >= 500:
        return {
            "status": "degraded",
            "endpoint": endpoint,
            "detail": f"health probe returned HTTP {response.status_code}",
            "instruction": (
                "The image-audit service reports a degraded state. Enqueue only if necessary and "
                "warn the user of possible delays."
            ),
        }

    # 2xx and 4xx (including the expected 404) both prove the gateway is up.
    return {
        "status": "healthy",
        "endpoint": endpoint,
        "instruction": (
            "The image-audit service is reachable. You may enqueue image audits as needed. "
            "Do NOT call this tool again in this turn."
        ),
    }



orchestrator_tools: list = [
    get_time,
    save_latest_image_request_id,
    get_latest_image_request_id,
    enque_request_for_auditing_stream,
    read_status_of_a_case,
    check_medgemma_health,
    read_soul_md,
    wikipedia_search,
    internet_search,
]



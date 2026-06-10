"""
Author: DermascanTeam
Date: 2026-04-20
Description: Orchestrator tools for the orchestrator Agent

"""

import os
import json
import time
from logging import Logger
from uuid import uuid4
from datetime import datetime
from dotenv import load_dotenv
from typing import Any

import httpx
from langchain.tools import tool
from redis import Redis
from tavily import TavilyClient

from dermascan.shared.logger import get_logger

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


_redis: Redis | None = Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
_AUDIT_ENQUEUED_KEY_PREFIX: str = "diagnose:enqueued"
_AUDIT_ENQUEUED_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
_INGRESS_PAYLOAD_CACHE_PREFIX: str = "orchestrator:ingress_payload"
_INGRESS_PAYLOAD_CACHE_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))



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
    return datetime.now().isoformat()


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
    logger.info(f"TOOL CALL: merged_payload={merged_payload}")

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
        }

    return {
        "status": "found",
        "case_id": case_id,
        "request_id": request_id,
        "latest": matches[0],
        "history": matches,
        "scanned": scanned,
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
    logger.info(f"TOOL CALL: read_soul_md")
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "agents", "soul.md")
    )
    with open(path, "r", encoding="utf-8") as f:
        contents = f.read()

    return contents




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
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    logger.info(f"TOOL CALL: wikipedia_search: query={query}, max_results={max_results}")
    try:
        capped_results = max(1, min(max_results, 5))
        result = client.search(
            query=query,
            max_results=capped_results,
            include_domains=["wikipedia.org"],
        )
        return _format_tavily_results(result, max_results=capped_results)
    except Exception as exc:
        return f"Wikipedia search failed: {exc}"


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
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    logger.info(f"TOOL CALL: internet_search: query={query}, max_results={max_results}")
    try:
        capped_results = max(1, min(max_results, 3))
        result = client.search(
            query=query,
            max_results=capped_results,
            search_depth="advanced",
            include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "mydawa.com"],
        )
        logger.info(f"TOOL CALL SUCCESS: internet_search: result={result}")
        return _format_tavily_results(result, max_results=capped_results)
    except Exception as exc:
        return f"Internet search failed: {exc}"



orchestrator_tools: list = [
    get_time,
    enque_request_for_auditing_stream,
    read_status_of_a_case,
    read_soul_md,
    wikipedia_search,
    internet_search,
]

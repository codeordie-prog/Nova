"""
Author: DermascanTeam
Date: 2026-04-20
Description: Orchestrator tools for the orchestrator Agent

"""

import os
import json
from logging import Logger
from uuid import uuid4
from datetime import datetime
from dotenv import load_dotenv
from typing import Any

from langchain.tools import tool
from redis import Redis
from tavily import TavilyClient

from dermascan.shared.logger import get_logger

load_dotenv()

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY_QUTIS", "")

# Initialize Logger
logger: Logger = get_logger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "")
DIAGNOSE_JOB_STREAM: str = os.getenv("DIAGNOSE_JOB_STREAM", "diagnose:jobs")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

CASE_STATUS_SCAN_PAGE: int = max(1, int(os.getenv("CASE_STATUS_SCAN_PAGE", "500")))
CASE_STATUS_SCAN_MAX: int = max(CASE_STATUS_SCAN_PAGE, int(os.getenv("CASE_STATUS_SCAN_MAX", "5000")))
CASE_STATUS_HISTORY: int = max(1, int(os.getenv("CASE_STATUS_HISTORY", "5")))
SEARCH_RESULT_CONTENT_CHARS: int = max(200, int(os.getenv("SEARCH_RESULT_CONTENT_CHARS", "700")))
SEARCH_RESULT_TOTAL_CHARS: int = max(1000, int(os.getenv("SEARCH_RESULT_TOTAL_CHARS", "4500")))


_redis: Redis | None = Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
_DIAGNOSE_ENQUEUED_KEY_PREFIX: str = "diagnose:enqueued"
_DIAGNOSE_ENQUEUED_TTL_SEC: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))
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


def enqueue_diagnose_job(
    *,
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    channel: str,
    payload: str,
    source: str = "orchestrator_tool",
) -> dict[str, str]:
    """
    Enqueue a diagnose job with the canonical ingress payload (idempotent per request_id).
    """
    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured"}

    rid = request_id or str(uuid4())
    mid = message_id or rid
    cid = client_id or "anonymous"
    csid = case_id or rid

    dedupe_key = f"{_DIAGNOSE_ENQUEUED_KEY_PREFIX}:{rid}"
    if not _redis.set(dedupe_key, "1", nx=True, ex=_DIAGNOSE_ENQUEUED_TTL_SEC):
        logger.info("diagnose job already enqueued: request_id=%s", rid)
        return {
            "status": "already_queued",
            "stream": DIAGNOSE_JOB_STREAM,
            "request_id": rid,
        }

    stream_id = _redis.xadd(
        DIAGNOSE_JOB_STREAM,
        {
            "request_id": rid,
            "case_id": csid,
            "client_id": cid,
            "message_id": mid,
            "channel": channel,
            "payload": payload,
            "source": source,
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )

    _redis.xadd(
        EVENTS_STREAM,
        {
            "request_id": rid,
            "case_id": csid,
            "client_id": cid,
            "message_id": mid,
            "channel": channel,
            "agent": "orchestrator",
            "status": "routed_to_diagnose",
            "target_stream": DIAGNOSE_JOB_STREAM,
            "target_stream_id": str(stream_id),
            "enqueue_source": source,
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )

    return {
        "status": "queued",
        "stream": DIAGNOSE_JOB_STREAM,
        "stream_id": str(stream_id),
        "request_id": rid,
    }


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
    Read cached ingress envelope fields needed for diagnose dispatch.

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
    Merge orchestrator-collected context into payload.content.text for diagnose.

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
    extras_obj["diagnose_prompt_enriched"] = "true"

    return json.dumps(payload_obj, default=str)


@tool
def get_time() -> str:
    """
    Useful when you need to know the current time
    """
    logger.info(f"TOOL CALL: get_time")
    return datetime.now().isoformat()


@tool
def enque_to_diagnose_stream(request_id: str, collected_context: str = "") -> dict[str, str]:
    """
    Route the current request to the diagnose worker.

    Args:
        request_id: The exact request_id shown in the current orchestrator input.
        collected_context: A concise structured summary for diagnosis. Include
            only facts gathered from the user, not raw chat or internal details.

    Returns:
        Queue result with status, stream, stream_id, and request_id.
    """

    logger.info(f"TOOL CALL: enque_to_diagnose_stream: request_id={request_id}, collected_context={collected_context}")

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
    merged_payload = _merge_collected_context_into_payload(canonical_payload, collected_context)
    logger.info(f"TOOL CALL: merged_payload={merged_payload}")

    return enqueue_diagnose_job(
        request_id=envelope["request_id"],
        case_id=envelope["case_id"],
        client_id=envelope["client_id"],
        message_id=envelope["message_id"],
        channel=envelope["channel"],
        payload=merged_payload,
        source="orchestrator_tool",
    )


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
    enque_to_diagnose_stream,
    read_status_of_a_case,
    read_soul_md,
    wikipedia_search,
    internet_search,
]

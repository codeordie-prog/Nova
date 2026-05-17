"""
Author: DermascanTeam
Date: 2026-04-20
Description: Orchestrator tools for the orchestrator Agent

"""

import json
import os
from uuid import uuid4
from datetime import datetime
from dotenv import load_dotenv

from langchain.tools import tool
from redis import Redis

from dermascan.backend.services.agents.memory.redis_checkpointer import default_redis_connection_kwargs
from dermascan.shared.logger import get_logger
from dermascan.shared.payload_log import dumps_compact, dumps_pretty

load_dotenv()

logger = get_logger(__name__)

REDIS_URL: str = os.getenv("REDIS_URL", "")
DIAGNOSE_JOB_STREAM: str = os.getenv("DIAGNOSE_JOB_STREAM", "diagnose:jobs")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

CASE_STATUS_SCAN_PAGE: int = max(1, int(os.getenv("CASE_STATUS_SCAN_PAGE", "500")))
CASE_STATUS_SCAN_MAX: int = max(CASE_STATUS_SCAN_PAGE, int(os.getenv("CASE_STATUS_SCAN_MAX", "5000")))
CASE_STATUS_HISTORY: int = max(1, int(os.getenv("CASE_STATUS_HISTORY", "5")))


_redis: Redis | None = (
    Redis.from_url(REDIS_URL, decode_responses=True, **default_redis_connection_kwargs())
    if REDIS_URL else None
)

@tool
def get_time(*args, **kwargs):
    """
    Useful when you need to know the current time
    """
    return datetime.now()


@tool
def enque_to_diagnose_stream(
    request_id: str,
    case_id: str,
    client_id: str,
    message_id: str,
    payload: str,
    channel: str,
    *args,
    **kwargs
) -> dict[str, str]:
    """
    Enqueue a normalized diagnose job into the Redis `diagnose:jobs` stream.

    Args:
        request_id: Unique request identifier for tracing the job across streams.
        case_id: Case identifier for diagnosis state tracking.
        client_id: End-user/client identifier. Keep this stable per user session.
        message_id: Source message identifier from the ingress channel.
        payload: Canonical request payload JSON string to forward to diagnose worker.
            Expected shape typically includes `content.text`, optional
            `content.attachments`, and metadata from orchestrator ingress.
        channel: Source channel name (for example: `whatsapp`, `web`, `telegram`).

    Returns:
        A dict with queue result:
        - `status`: `queued` or `error`
        - `stream`: target stream name
        - `stream_id`: Redis stream entry id when queued
        - `request_id`: request id used for the enqueued job
    """
    if _redis is None:
        return {"status": "error", "detail": "REDIS_URL is not configured"}

    rid = request_id or str(uuid4())
    mid = message_id or rid
    cid = client_id or "anonymous"
    csid = case_id or rid

    # Inspect the payload the orchestrator agent (LLM) built. This is the point
    # where a dropped / mangled image attachment first becomes visible — the LLM
    # passes `payload` as a free-form string, so it can lose `content.attachments`.
    try:
        parsed_payload = json.loads(payload) if payload else {}
    except Exception:
        parsed_payload = {}
    payload_attachments = ((parsed_payload.get("content") or {}).get("attachments")) or []
    attachment_summary = [
        {
            "type": a.get("type"),
            "mime_type": a.get("mime_type"),
            "source_kind": (a.get("source") or {}).get("kind"),
            "source_value_prefix": str((a.get("source") or {}).get("value") or "")[:120],
        }
        for a in payload_attachments
        if isinstance(a, dict)
    ]
    logger.info(
        "[ENQUEUE-DIAGNOSE] tool called  request_id=%s  case_id=%s  client_id=%s  message_id=%s  "
        "channel=%s\n  payload_attachments=%d  attachment_summary=%s\n  full_payload=\n%s",
        rid,
        csid,
        cid,
        mid,
        channel,
        len(payload_attachments),
        dumps_compact(attachment_summary),
        dumps_pretty(parsed_payload) if parsed_payload else payload,
    )
    if not payload_attachments:
        logger.warning(
            "[ENQUEUE-DIAGNOSE] request_id=%s  payload has NO attachments — the diagnose worker "
            "will report 'diagnose job missing image'. The orchestrator agent dropped the image.",
            rid,
        )

    stream_id = _redis.xadd(
        DIAGNOSE_JOB_STREAM,
        {
            "request_id": rid,
            "case_id": csid,
            "client_id": cid,
            "message_id": mid,
            "channel": channel,
            "payload": payload,
            "source": "orchestrator_tool",
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )
    logger.info(
        "[ENQUEUE-DIAGNOSE] queued to %s  stream_id=%s  request_id=%s",
        DIAGNOSE_JOB_STREAM,
        stream_id,
        rid,
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
def read_status_of_a_case(case_id: str, request_id: str = "", *args, **kwargs) -> dict:
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
def read_soul_md(*args, **kwargs) -> str:
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

    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "agents", "soul.md")
    )
    with open(path, "r", encoding="utf-8") as f:
        contents = f.read()

    return contents



orchestrator_tools: list = [
    get_time,
    enque_to_diagnose_stream,
    read_status_of_a_case,
    read_soul_md,
]

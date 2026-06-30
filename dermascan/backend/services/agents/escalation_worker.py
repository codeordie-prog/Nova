"""Escalation worker: consume escalation:jobs, enrich, relay to dermatologist platform."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import redis
import requests
from dotenv import load_dotenv
from pydantic import BaseModel

from dermascan.backend.services.agents import reclaim_logic
from dermascan.shared.hmac_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, signature_header
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)

load_dotenv()

logger = get_logger("agents")

REDIS_URL: str = os.getenv("REDIS_URL", "")
EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")
ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
ESCALATION_STREAM: str = os.getenv("ESCALATION_STREAM", "escalation:jobs")
ESCALATION_DLQ_STREAM: str = os.getenv("ESCALATION_DLQ_STREAM", "escalation:dlq")
ESCALATION_CONSUMER_GROUP: str = os.getenv("ESCALATION_CONSUMER_GROUP", "escalation-workers")
ESCALATION_CONSUMER_NAME_BASE: str = os.getenv("ESCALATION_CONSUMER_NAME", "escalation-worker")
ESCALATION_CONSUMER_NAME: str = f"{ESCALATION_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
ESCALATION_PLATFORM_URL: str = os.getenv("ESCALATION_PLATFORM_URL", "").strip()
ESCALATION_RELAY_MAX_ATTEMPTS: int = max(1, int(os.getenv("ESCALATION_RELAY_MAX_ATTEMPTS", "3")))
ESCALATION_RELAY_BASE_DELAY_SEC: float = max(0.0, float(os.getenv("ESCALATION_RELAY_BASE_DELAY_SEC", "0.75")))
ESCALATION_CONTEXT_MESSAGE_N: int = max(
    1,
    int(
        os.getenv(
            "ESCALATION_CONTEXT_MESSAGE_N",
            os.getenv("CONVERSATION_LOG_INCLUDE_N", "10"),
        )
    ),
)
CONVERSATION_LOG_KEY_PREFIX: str = os.getenv("CONVERSATION_LOG_KEY_PREFIX", "orchestrator:conversation_log")
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
WORKERS_STREAM_BLOCK_MS: int = int(os.getenv("WORKERS_STREAM_BLOCK_MS", "500"))
WORKERS_STREAM_BATCH_SIZE: int = max(1, int(os.getenv("WORKERS_STREAM_BATCH_SIZE", "8")))
WORKERS_MAX_INFLIGHT: int = max(1, int(os.getenv("WORKERS_MAX_INFLIGHT", "8")))
SIGNAL_PAYLOAD_TTL_SEC: int = 7 * 24 * 3600
IMAGE_PATH_TTL_SEC: int = 7 * 24 * 3600
RELAY_IDEMPOTENCY_TTL_SEC: int = 86400

if not REDIS_URL:
    raise RuntimeError("REDIS_URL is required for the escalation worker")

_redis: redis.Redis = get_redis_client(decode_responses=True)
reclaim_logic.set_redis_client(_redis)


class EscalationJobPayload(BaseModel):
    schema_version: str = "v1"
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    source_agent: str = "medgemma"
    mode: str
    reason: str = "low_confidence"
    confidence: float | None = None
    threshold: float | None = None
    requires_human_review: bool = True
    diagnosis_text: str = ""
    product_name: str = ""
    description: str = ""
    indications: str = ""
    safety_in_pregnancy: str = ""
    contraindications: str = ""
    how_to_use: str = ""
    special_instructions: str = ""
    image_blob_path: str = ""
    review_type: str = ""
    rationale: str = ""
    summary: str = ""
    treatment: str = ""


def _ensure_consumer_group() -> None:
    try:
        _redis.xgroup_create(ESCALATION_STREAM, ESCALATION_CONSUMER_GROUP, id="0", mkstream=True)
        logger.info(
            "escalation consumer group created: stream=%s group=%s",
            ESCALATION_STREAM,
            ESCALATION_CONSUMER_GROUP,
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _emit_event(fields: dict[str, Any]) -> None:
    flat = {k: "" if v is None else str(v) for k, v in fields.items()}
    _redis.xadd(
        EVENTS_STREAM,
        flat,
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )


def _conversation_log_key(client_id: str) -> str:
    return f"{CONVERSATION_LOG_KEY_PREFIX}:{client_id}"


def _read_conversation_log(client_id: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        raw_entries = run_with_redis_retries(
            lambda: _redis.lrange(_conversation_log_key(client_id), 0, limit - 1),
            operation_name="escalation_conversation_log",
        )
    except Exception:
        logger.exception("failed to read conversation log: client_id=%s", client_id)
        return []

    parsed: list[dict[str, Any]] = []
    for raw in raw_entries or []:
        try:
            parsed.append(json.loads(raw))
        except Exception:
            continue
    return parsed


def _parse_job(fields: dict[str, Any]) -> EscalationJobPayload:
    confidence_raw = str(fields.get("confidence") or "").strip()
    threshold_raw = str(fields.get("threshold") or "").strip()
    return EscalationJobPayload(
        schema_version=str(fields.get("schema_version") or "v1"),
        request_id=str(fields.get("request_id") or ""),
        case_id=str(fields.get("case_id") or ""),
        client_id=str(fields.get("client_id") or ""),
        message_id=str(fields.get("message_id") or ""),
        channel=str(fields.get("channel") or ""),
        source_agent=str(fields.get("source_agent") or "medgemma"),
        mode=str(fields.get("mode") or "diagnose"),
        reason=str(fields.get("reason") or "low_confidence"),
        confidence=float(confidence_raw) if confidence_raw else None,
        threshold=float(threshold_raw) if threshold_raw else None,
        requires_human_review=str(fields.get("requires_human_review") or "true").strip().lower() == "true",
        diagnosis_text=str(fields.get("diagnosis_text") or ""),
        product_name=str(fields.get("product_name") or ""),
        description=str(fields.get("description") or ""),
        indications=str(fields.get("indications") or ""),
        safety_in_pregnancy=str(fields.get("safety_in_pregnancy") or ""),
        contraindications=str(fields.get("contraindications") or ""),
        how_to_use=str(fields.get("how_to_use") or ""),
        special_instructions=str(fields.get("special_instructions") or ""),
        image_blob_path=str(fields.get("image_blob_path") or ""),
        review_type=str(fields.get("review_type") or ""),
        rationale=str(fields.get("rationale") or ""),
        summary=str(fields.get("summary") or ""),
        treatment=str(fields.get("treatment") or ""),
    )


def _parse_json_obj(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _humanize_diagnosis(raw: Any) -> str:
    """Return clean, human-readable diagnosis prose — never a JSON blob.

    MedGemma's ``diagnosis_text`` is sometimes the whole structured JSON (when the
    inner ``diagnosis`` field is empty, e.g. at a 100% escalation threshold). Surface
    only the inner prose; if there is none, return "" so the UI/outbound shows a clean
    placeholder instead of raw JSON.
    """
    if not raw:
        return ""
    obj: Any = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return ""
        if s[0] not in "{[":
            return s  # already plain prose
        try:
            obj = json.loads(s)
        except Exception:
            return ""  # looked like JSON but unparseable → drop, never leak
    if isinstance(obj, dict):
        for key in ("diagnosis", "rationale"):
            value = str(obj.get(key) or "").strip()
            if value:
                return value
        return ""
    return ""


def _build_escalated_signal_payload(job: EscalationJobPayload) -> dict[str, Any]:
    """Build the full-fidelity ``{mode}_escalated`` payload from job fields alone.

    The producer carries ``summary``/``treatment``/``rationale``/``review_type`` on
    ``escalation:jobs`` (escalation.py ``_push_escalation``), so this is deterministic
    — no race against the orchestrator consuming/deleting the signal.
    """
    return {
        "review_type": job.review_type,
        "diagnosis": _humanize_diagnosis(job.diagnosis_text),
        "diagnosis_text": _humanize_diagnosis(job.diagnosis_text),
        "rationale": job.rationale,
        "summary": _parse_json_obj(job.summary),
        "treatment": _parse_json_obj(job.treatment),
        "confidence": job.confidence,
        "threshold": job.threshold,
        "escalation": True,
        "mode": job.mode,
        "product_name": job.product_name,
        "description": job.description,
        "indications": job.indications,
        "safety_in_pregnancy": job.safety_in_pregnancy,
        "contraindications": job.contraindications,
        "how_to_use": job.how_to_use,
        "special_instructions": job.special_instructions,
        "metadata": {"extras": {"group_id": "", "group_expected_count": "1"}},
    }


def _cache_signal_payload(job: EscalationJobPayload, payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["request_id"] = job.request_id
    enriched["case_id"] = job.case_id
    enriched["client_id"] = job.client_id
    enriched["message_id"] = job.message_id
    enriched["channel"] = job.channel
    _redis.setex(
        f"escalation:signal_payload:{job.case_id}",
        SIGNAL_PAYLOAD_TTL_SEC,
        json.dumps(enriched, default=str),
    )
    if job.image_blob_path:
        _redis.setex(
            f"escalation:image_path:{job.case_id}",
            IMAGE_PATH_TTL_SEC,
            job.image_blob_path,
        )
    _redis.delete(f"escalation:closed:{job.case_id}")
    return enriched


def _build_handoff_packet(job: EscalationJobPayload, context: list[dict[str, Any]]) -> dict[str, Any]:
    chronological = list(reversed(context))
    return {
        "case": {
            "request_id": job.request_id,
            "case_id": job.case_id,
            "client_id": job.client_id,
            "channel": job.channel,
            "mode": job.mode,
            "confidence": job.confidence,
            "threshold": job.threshold,
            "diagnosis_text": _humanize_diagnosis(job.diagnosis_text),
            "product_name": job.product_name,
            "description": job.description,
        },
        "conversation_context": [
            {
                "role": entry.get("role", ""),
                "text": entry.get("text", ""),
                "ts_ms": entry.get("ts_ms", 0),
                "has_image": bool(entry.get("has_image", False)),
            }
            for entry in chronological
        ],
    }


def _relay_handoff(handoff: dict[str, Any]) -> None:
    if not ESCALATION_PLATFORM_URL:
        raise RuntimeError("ESCALATION_PLATFORM_URL is required for escalation relay")

    body = json.dumps(handoff, default=str).encode("utf-8")
    last_exc: Exception | None = None

    for attempt in range(1, ESCALATION_RELAY_MAX_ATTEMPTS + 1):
        sig, ts = signature_header(body)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sig,
            TIMESTAMP_HEADER: ts,
        }
        try:
            response = requests.post(
                ESCALATION_PLATFORM_URL,
                data=body,
                headers=headers,
                timeout=30,
            )
            if 200 <= response.status_code < 300:
                return
            last_exc = RuntimeError(f"platform returned {response.status_code}: {response.text[:200]}")
        except Exception as exc:
            last_exc = exc

        if attempt < ESCALATION_RELAY_MAX_ATTEMPTS:
            delay = ESCALATION_RELAY_BASE_DELAY_SEC * (2 ** (attempt - 1))
            _emit_event(
                {
                    "request_id": handoff["case"]["request_id"],
                    "case_id": handoff["case"]["case_id"],
                    "client_id": handoff["case"]["client_id"],
                    "message_id": "",
                    "channel": handoff["case"]["channel"],
                    "agent": "escalation",
                    "mode": handoff["case"]["mode"],
                    "status": "escalation_relay_retry",
                    "attempt": str(attempt),
                    "max_attempts": str(ESCALATION_RELAY_MAX_ATTEMPTS),
                    "error": str(last_exc),
                    "retry_delay_sec": str(delay),
                }
            )
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("escalation relay failed without exception")


def _enqueue_escalation_deferred(cached_payload: dict[str, Any], job: EscalationJobPayload) -> None:
    mode = str(cached_payload.get("mode") or job.mode)
    _redis.xadd(
        ORCHESTRATOR_SIGNAL_STREAM,
        {
            "signal_type": f"{mode}_escalation_deferred",
            "signal_id": uuid.uuid4().hex,
            "request_id": job.request_id,
            "case_id": job.case_id,
            "client_id": job.client_id,
            "message_id": job.message_id,
            "channel": job.channel,
            "source_event_id": "",
            "payload": json.dumps(cached_payload, default=str),
            "notes": "Escalation relay deferred; specialist review pending.",
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )


def _write_dlq(job: EscalationJobPayload, msg_id: str, fields: dict[str, Any], error: str) -> None:
    _redis.xadd(
        ESCALATION_DLQ_STREAM,
        {
            "request_id": job.request_id,
            "case_id": job.case_id,
            "client_id": job.client_id,
            "message_id": job.message_id,
            "channel": job.channel,
            "source_stream": ESCALATION_STREAM,
            "source_msg_id": msg_id,
            "error": error,
            "payload": json.dumps(fields, default=str),
            "failed_at_ms": str(int(time.time() * 1000)),
        },
        maxlen=WORKERS_STREAM_MAXLEN,
        approximate=True,
    )


def _ack_and_delete(msg_id: str) -> None:
    _redis.xack(ESCALATION_STREAM, ESCALATION_CONSUMER_GROUP, msg_id)
    _redis.xdel(ESCALATION_STREAM, msg_id)


def _process_escalation_job(msg_id: str, fields: dict[str, Any]) -> None:
    job = _parse_job(fields)
    _emit_event(
        {
            "request_id": job.request_id,
            "case_id": job.case_id,
            "client_id": job.client_id,
            "message_id": job.message_id,
            "channel": job.channel,
            "agent": "escalation",
            "mode": job.mode,
            "status": "escalation_job_consumed",
        }
    )

    try:
        relay_key = f"escalation:relayed:{job.case_id}"
        if not _redis.set(relay_key, "1", nx=True, ex=RELAY_IDEMPOTENCY_TTL_SEC):
            logger.warning(
                "escalation relay skipped (already relayed): case_id=%s msg_id=%s",
                job.case_id,
                msg_id,
            )
            _ack_and_delete(msg_id)
            reclaim_logic.mark_processed(ESCALATION_STREAM, msg_id)
            return

        context = _read_conversation_log(job.client_id, ESCALATION_CONTEXT_MESSAGE_N)
        signal_payload = _build_escalated_signal_payload(job)
        cached_payload = _cache_signal_payload(job, signal_payload)
        handoff = _build_handoff_packet(job, context)

        try:
            _relay_handoff(handoff)
        except Exception as relay_exc:
            # Relay failed: clear the idempotency guard so a future re-escalation of
            # this case can retry (it was set NX before relay, not after success).
            logger.warning(
                "escalation relay failed → DLQ + deferred: case_id=%s err=%s",
                job.case_id,
                relay_exc,
            )
            _redis.delete(relay_key)
            _write_dlq(job, msg_id, fields, str(relay_exc))
            _emit_event(
                {
                    "request_id": job.request_id,
                    "case_id": job.case_id,
                    "client_id": job.client_id,
                    "message_id": job.message_id,
                    "channel": job.channel,
                    "agent": "escalation",
                    "mode": job.mode,
                    "status": "escalation_relay_failed",
                    "error": str(relay_exc),
                }
            )
            _enqueue_escalation_deferred(cached_payload, job)
            _ack_and_delete(msg_id)
            return

        _emit_event(
            {
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "message_id": job.message_id,
                "channel": job.channel,
                "agent": "escalation",
                "mode": job.mode,
                "status": "escalation_relay_completed",
            }
        )
        _ack_and_delete(msg_id)
        reclaim_logic.mark_processed(ESCALATION_STREAM, msg_id)
    except Exception as exc:
        try:
            _redis.delete(f"escalation:relayed:{job.case_id}")
        except Exception:
            logger.exception("failed to clear relay idempotency key: case_id=%s", job.case_id)
        _write_dlq(job, msg_id, fields, str(exc))
        _emit_event(
            {
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "message_id": job.message_id,
                "channel": job.channel,
                "agent": "escalation",
                "mode": job.mode,
                "status": "escalation_relay_failed",
                "error": str(exc),
            }
        )
        try:
            cached_raw = _redis.get(f"escalation:signal_payload:{job.case_id}")
            cached_payload = json.loads(cached_raw) if cached_raw else _build_escalated_signal_payload(job)
            if not cached_raw:
                _cache_signal_payload(job, cached_payload)
            _enqueue_escalation_deferred(cached_payload, job)
        except Exception:
            logger.exception("failed to enqueue deferred signal: case_id=%s", job.case_id)
        _ack_and_delete(msg_id)
        logger.exception("escalation job failed: case_id=%s", job.case_id)


def _start_reclaim_thread(stop_event: threading.Event) -> threading.Thread:
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:
                def _reclaim_handler(reclaim_msg_id: str, reclaim_fields: dict[str, Any]) -> None:
                    if reclaim_logic.mark_processed(ESCALATION_STREAM, reclaim_msg_id):
                        try:
                            _process_escalation_job(reclaim_msg_id, reclaim_fields)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                ESCALATION_STREAM,
                                reclaim_msg_id,
                            )
                            _redis.delete(reclaim_logic.processed_key(ESCALATION_STREAM, reclaim_msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=ESCALATION_STREAM,
                    group=ESCALATION_CONSUMER_GROUP,
                    consumer=ESCALATION_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=ESCALATION_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        ESCALATION_STREAM,
                        reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", ESCALATION_STREAM)
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"escalation-reclaim-{ESCALATION_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


def run_worker_forever() -> None:
    check_redis_connectivity(decode_responses=True)
    _ensure_consumer_group()
    logger.info(
        "escalation worker started: stream=%s group=%s consumer=%s",
        ESCALATION_STREAM,
        ESCALATION_CONSUMER_GROUP,
        ESCALATION_CONSUMER_NAME,
    )

    stop_event = threading.Event()
    reclaim_thread = _start_reclaim_thread(stop_event)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS_MAX_INFLIGHT) as pool:
            while not stop_event.is_set():
                try:
                    resp = run_with_redis_retries(
                        lambda: _redis.xreadgroup(
                            groupname=ESCALATION_CONSUMER_GROUP,
                            consumername=ESCALATION_CONSUMER_NAME,
                            streams={ESCALATION_STREAM: ">"},
                            count=WORKERS_STREAM_BATCH_SIZE,
                            block=WORKERS_STREAM_BLOCK_MS,
                        ),
                        operation_name="escalation_xreadgroup",
                    )
                except Exception:
                    logger.exception("escalation xreadgroup failed")
                    time.sleep(0.25)
                    continue

                messages = [
                    (str(msg_id), fields)
                    for _, batch in (resp or [])
                    for msg_id, fields in batch
                ]
                for msg_id, fields in messages:
                    pool.submit(_process_escalation_job, msg_id, fields)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        reclaim_thread.join(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC + 1)


if __name__ == "__main__":
    run_worker_forever()

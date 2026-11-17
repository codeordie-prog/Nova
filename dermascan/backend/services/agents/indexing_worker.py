"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-24
Licence: PolyForm Noncommercial License 1.0.0

Consume ``rules:index:jobs``, run CIR PDF lazy indexing off the orchestrator hot path,
and emit ``product_review_index_ready`` when a request batch completes.
"""


from __future__ import annotations

import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import redis

from dermascan.backend.services.agents import reclaim_logic
from dermascan.backend.services.agents.heartbeat import (
    compute_heartbeat_interval_sec,
    start_stream_heartbeat,
)
from dermascan.backend.services.agents.rules_index_jobs import (
    RULES_INDEX_CONSUMER_GROUP,
    RULES_INDEX_DLQ_STREAM,
    RULES_INDEX_STREAM,
    RulesIndexJobPayload,
    clear_rules_index_job_dedupe,
    ensure_rules_index_consumer_group,
    finalize_index_batch_if_complete,
    mark_ingredient_index_cache,
    parse_job_fields,
)
from dermascan.rules_engine.vector_store.build_vectorstore import VectorStore
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)

logger = get_logger("agents")

EVENTS_STREAM: str = os.getenv("EVENTS_STREAM", "events:stream")

RULES_INDEX_CONSUMER_NAME_BASE: str = os.getenv("RULES_INDEX_CONSUMER_NAME", "rules-index-worker")
RULES_INDEX_CONSUMER_NAME: str = f"{RULES_INDEX_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
INDEX_WORKER_MAX_INFLIGHT: int = max(1, int(os.getenv("INDEX_WORKER_MAX_INFLIGHT", "1")))
WORKERS_STREAM_BLOCK_MS: int = int(os.getenv("WORKERS_STREAM_BLOCK_MS", "500"))
WORKERS_STREAM_BATCH_SIZE: int = max(1, int(os.getenv("WORKERS_STREAM_BATCH_SIZE", "8")))
WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

INDEX_HEARTBEAT_INTERVAL_SEC: float = compute_heartbeat_interval_sec(
    reclaim_logic.PENDING_MIN_IDLE_MS,
)

_redis: redis.Redis = get_redis_client(decode_responses=True)
reclaim_logic.set_redis_client(_redis)
_vector_store: VectorStore | None = None


def _get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store


def _ack_and_delete(msg_id: str) -> None:
    run_with_redis_retries(
        lambda: _redis.xack(RULES_INDEX_STREAM, RULES_INDEX_CONSUMER_GROUP, msg_id),
        operation_name="rules_index_xack",
    )
    run_with_redis_retries(
        lambda: _redis.xdel(RULES_INDEX_STREAM, msg_id),
        operation_name="rules_index_xdel",
    )


def _emit_index_event(fields: dict[str, Any]) -> None:
    """
    Publish a lifecycle event for the rules index worker.

    Args:
        fields: Event payload (request_id, status, etc.).
    """
    try:
        run_with_redis_retries(
            lambda: _redis.xadd(
                EVENTS_STREAM,
                fields,
                maxlen=WORKERS_STREAM_MAXLEN,
                approximate=True,
            ),
            operation_name="rules_index_event_xadd",
        )
    except Exception:
        logger.exception("failed to emit rules index event: fields=%s", list(fields.keys()))


def _write_dlq(msg_id: str, fields: dict[str, Any], error: str) -> None:
    run_with_redis_retries(
        lambda: _redis.xadd(
            RULES_INDEX_DLQ_STREAM,
            {
                "source_stream": RULES_INDEX_STREAM,
                "source_msg_id": msg_id,
                "error": error[:2000],
                "fields": json.dumps(fields, default=str)[:8000],
                "failed_at_ms": str(int(time.time() * 1000)),
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        ),
        operation_name="rules_index_dlq_xadd",
    )


def _run_index_job(job: RulesIndexJobPayload) -> None:
    """
    Index one CIR PDF and mark the ingredient cache entry.

    Args:
        job: Parsed index stream payload.

    Raises:
        Exception: Propagates vector store / network failures.
    """
    store = _get_vector_store()
    store.index_pdf_from_firebase(
        pdf_url=job.pdf_url,
        ingredient_id=job.ingredient_id,
        cir_ingredient_name=job.cir_ingredient_name,
        function=job.function,
        report_url=job.report_url,
        attachment_url=job.attachment_url or job.pdf_url,
        storage_path=job.storage_path or job.pdf_url,
    )
    mark_ingredient_index_cache(
        _redis,
        normalized_name=job.normalized_name,
        ingredient_id=job.ingredient_id,
    )


def _process_index_job(msg_id: str, fields: dict[str, Any]) -> None:
    """
    Process one rules index stream message.

    Args:
        msg_id: Redis stream message id.
        fields: Raw stream fields.
    """
    job = parse_job_fields(fields)
    heartbeat_stop = start_stream_heartbeat(
        _redis,
        stream=RULES_INDEX_STREAM,
        group=RULES_INDEX_CONSUMER_GROUP,
        consumer=RULES_INDEX_CONSUMER_NAME,
        msg_id=msg_id,
        interval_sec=INDEX_HEARTBEAT_INTERVAL_SEC,
    )
    job_succeeded = False
    started_at = time.monotonic()
    batch_signal_emitted = False
    try:
        logger.info(
            "rules index job started: msg_id=%s request_id=%s normalized=%s",
            msg_id,
            job.request_id,
            job.normalized_name,
        )
        _emit_index_event(
            {
                "agent": "rules_index",
                "status": "rules_index_job_started",
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "normalized_name": job.normalized_name,
                "msg_id": msg_id,
            }
        )
        _run_index_job(job)
        job_succeeded = True
        duration_s = time.monotonic() - started_at
        logger.info(
            "rules index job completed: msg_id=%s request_id=%s normalized=%s duration_s=%.2f",
            msg_id,
            job.request_id,
            job.normalized_name,
            duration_s,
        )
        _emit_index_event(
            {
                "agent": "rules_index",
                "status": "rules_index_job_completed",
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "normalized_name": job.normalized_name,
                "duration_s": f"{duration_s:.3f}",
            }
        )
    except Exception as exc:
        duration_s = time.monotonic() - started_at
        logger.exception(
            "rules index job failed: msg_id=%s request_id=%s normalized=%s duration_s=%.2f",
            msg_id,
            job.request_id,
            job.normalized_name,
            duration_s,
        )
        _write_dlq(msg_id, fields, str(exc))
        _emit_index_event(
            {
                "agent": "rules_index",
                "status": "rules_index_job_failed",
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "normalized_name": job.normalized_name,
                "duration_s": f"{duration_s:.3f}",
                "error": str(exc)[:500],
            }
        )
    finally:
        heartbeat_stop.set()
        clear_rules_index_job_dedupe(_redis, job.normalized_name)
        try:
            batch_signal_emitted = finalize_index_batch_if_complete(
                _redis,
                job,
                job_succeeded=job_succeeded,
            )
            if batch_signal_emitted:
                _emit_index_event(
                    {
                        "agent": "rules_index",
                        "status": "rules_index_batch_ready_signal",
                        "request_id": job.request_id,
                        "case_id": job.case_id,
                        "client_id": job.client_id,
                    }
                )
        except Exception:
            logger.exception(
                "index batch finalize failed: request_id=%s msg_id=%s",
                job.request_id,
                msg_id,
            )
        _ack_and_delete(msg_id)
        reclaim_logic.mark_processed(RULES_INDEX_STREAM, msg_id)


def _start_reclaim_thread(stop_event: threading.Event) -> threading.Thread:
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:

                def _reclaim_handler(reclaim_msg_id: str, reclaim_fields: dict[str, Any]) -> None:
                    if reclaim_logic.mark_processed(RULES_INDEX_STREAM, reclaim_msg_id):
                        try:
                            _process_index_job(reclaim_msg_id, reclaim_fields)
                        except Exception:
                            logger.exception(
                                "rules index reclaim handler failed: msg_id=%s",
                                reclaim_msg_id,
                            )
                            _redis.delete(reclaim_logic.processed_key(RULES_INDEX_STREAM, reclaim_msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=RULES_INDEX_STREAM,
                    group=RULES_INDEX_CONSUMER_GROUP,
                    consumer=RULES_INDEX_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=RULES_INDEX_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "rules index reclaim pass completed: stream=%s reclaimed=%d",
                        RULES_INDEX_STREAM,
                        reclaimed,
                    )
            except Exception:
                logger.exception("rules index reclaim loop error: stream=%s", RULES_INDEX_STREAM)
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"rules-index-reclaim-{RULES_INDEX_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


def run_worker_forever() -> None:
    """
    Run the CIR index worker until interrupted.

    Raises:
        RuntimeError: When Redis connectivity checks fail at startup.
    """
    check_redis_connectivity(decode_responses=True)
    ensure_rules_index_consumer_group(_redis)
    logger.info(
        "rules index worker started: stream=%s group=%s consumer=%s max_inflight=%s heartbeat_s=%.1f",
        RULES_INDEX_STREAM,
        RULES_INDEX_CONSUMER_GROUP,
        RULES_INDEX_CONSUMER_NAME,
        INDEX_WORKER_MAX_INFLIGHT,
        INDEX_HEARTBEAT_INTERVAL_SEC,
    )

    stop_event = threading.Event()
    reclaim_thread = _start_reclaim_thread(stop_event)

    try:
        with ThreadPoolExecutor(max_workers=INDEX_WORKER_MAX_INFLIGHT) as pool:
            while not stop_event.is_set():
                try:
                    resp = run_with_redis_retries(
                        lambda: _redis.xreadgroup(
                            groupname=RULES_INDEX_CONSUMER_GROUP,
                            consumername=RULES_INDEX_CONSUMER_NAME,
                            streams={RULES_INDEX_STREAM: ">"},
                            count=WORKERS_STREAM_BATCH_SIZE,
                            block=WORKERS_STREAM_BLOCK_MS,
                        ),
                        operation_name="rules_index_xreadgroup",
                    )
                except Exception:
                    logger.exception("rules index xreadgroup failed")
                    time.sleep(0.25)
                    continue

                messages = [
                    (str(msg_id), field_map)
                    for _, batch in (resp or [])
                    for msg_id, field_map in batch
                ]
                for msg_id, field_map in messages:
                    pool.submit(_process_index_job, msg_id, field_map)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        reclaim_thread.join(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC + 1)


if __name__ == "__main__":
    run_worker_forever()

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-24
License: MIT

Enqueue and coordinate async CIR ingredient indexing jobs on Redis streams.

Index workers consume ``RULES_INDEX_STREAM``; product rules enqueue work here instead
of calling ``index_pdf_from_firebase`` on orchestrator signal threads.
"""


from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

import redis
from pydantic import BaseModel, Field

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import run_with_redis_retries

logger = get_logger(__name__)

RULES_INDEX_STREAM: str = os.getenv("RULES_INDEX_STREAM", "rules:index:jobs")
RULES_INDEX_DLQ_STREAM: str = os.getenv("RULES_INDEX_DLQ_STREAM", "rules:index:jobs:dlq")
RULES_INDEX_CONSUMER_GROUP: str = os.getenv("RULES_INDEX_CONSUMER_GROUP", "rules-index-workers")

WORKERS_STREAM_MAXLEN: int = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))
RULES_INDEX_JOB_DEDUPE_TTL_SEC: int = max(
    60,
    int(os.getenv("RULES_INDEX_JOB_DEDUPE_TTL_SEC", str(24 * 3600))),
)

RULES_INDEX_QUEUED_KEY_PREFIX: Final[str] = "rules:index:queued:"
RULES_INDEX_BATCH_KEY_PREFIX: Final[str] = "rules:index:batch:"
RULES_INDEX_BATCH_FAILED_KEY_PREFIX: Final[str] = "rules:index:batch_failed:"
RULES_INDEX_CONTEXT_KEY_PREFIX: Final[str] = "rules:index:context:"
RULES_INDEX_READY_SIGNAL_KEY_PREFIX: Final[str] = "rules:index:ready_signal:"

ORCHESTRATOR_SIGNAL_STREAM: str = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
RULES_ENGINE_INGREDIENT_CACHE_PREFIX: str = os.getenv(
    "RULES_ENGINE_INGREDIENT_CACHE_PREFIX",
    "rules_engine:indexed",
)
RULES_INDEX_READY_SIGNAL_TTL_SEC: int = max(
    3600,
    int(os.getenv("RULES_INDEX_READY_SIGNAL_TTL_SEC", str(7 * 24 * 3600))),
)
RULES_INDEX_CONTEXT_TTL_SEC: int = max(
    3600,
    int(os.getenv("RULES_INDEX_CONTEXT_TTL_SEC", str(7 * 24 * 3600))),
)
RULES_INDEX_BATCH_TIMEOUT_SEC: int = max(
    60,
    int(os.getenv("RULES_INDEX_BATCH_TIMEOUT_SEC", "3600")),
)

PRODUCT_INDEX_PENDING_SENT_KEY_PREFIX: Final[str] = "rules:product_index_pending_sent:"
PRODUCT_REPORT_SENT_KEY_PREFIX: Final[str] = "rules:product_report_sent:"

ORCHESTRATOR_SIGNAL_TYPE_INDEX_READY: Final[str] = "product_review_index_ready"

PRODUCT_INDEX_PENDING_MESSAGE: str = os.getenv(
    "PRODUCT_INDEX_PENDING_MESSAGE",
    "We are examining the ingredients and safety profile for {product_name} against "
    "reference data. This usually takes a few minutes — we will notify you here when "
    "your product safety report is ready. This is educational guidance, not medical advice.",
)

RULES_INDEX_BATCH_TTL_SEC: int = max(
    3600,
    int(os.getenv("RULES_INDEX_BATCH_TTL_SEC", str(7 * 24 * 3600))),
)


def build_product_index_pending_message(product_name: str) -> str:
    """
    Build the interim user message while async CIR indexing runs.

    Args:
        product_name: Product label name when known.

    Returns:
        str: WhatsApp-ready interim copy.
    """
    display_name = product_name.strip() or "your product"
    if "{product_name}" in PRODUCT_INDEX_PENDING_MESSAGE:
        return PRODUCT_INDEX_PENDING_MESSAGE.format(product_name=display_name)
    return PRODUCT_INDEX_PENDING_MESSAGE


def rules_index_inline_enabled() -> bool:
    """
    Return True when CIR PDF indexing runs synchronously in the rules engine.

    When False, missing ingredients are enqueued on ``RULES_INDEX_STREAM``.

    Returns:
        bool: True if ``RULES_INDEX_INLINE`` is enabled (default until rollout).
    """
    return os.getenv("RULES_INDEX_INLINE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class RulesIndexScheduleContext(BaseModel):
    """
    Product-review identity fields required to enqueue index jobs and track batches.
    """

    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str


class RulesIndexJobPayload(BaseModel):
    """
    One CIR PDF indexing job for the rules index worker.

    Carries enough metadata to call ``VectorStore.index_pdf_from_firebase`` without
    re-querying Postgres.
    """

    schema_version: str = Field(default="v1")

    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str

    ingredient: str
    normalized_name: str
    ingredient_id: str

    pdf_url: str
    cir_ingredient_name: str
    function: str = "unknown"
    report_url: str = ""
    attachment_url: str = ""
    storage_path: str = ""


@dataclass(frozen=True)
class EnqueueRulesIndexJobResult:
    """
    Outcome of attempting to enqueue one index job.

    Args:
        enqueued: True when a new stream message was added.
        skipped_dedupe: True when dedupe key indicated a job already queued/in flight.
        message_id: Redis stream message id when enqueued.
    """

    enqueued: bool
    skipped_dedupe: bool
    message_id: str | None = None


def rules_index_queued_key(normalized_name: str) -> str:
    """
    Redis key that dedupes enqueue for one normalized ingredient name.

    Args:
        normalized_name: Normalized INCI lookup key.

    Returns:
        str: Dedupe key.
    """
    return f"{RULES_INDEX_QUEUED_KEY_PREFIX}{normalized_name.strip().lower()}"


def build_index_job_payload(
    schedule: RulesIndexScheduleContext,
    *,
    ingredient: str,
    normalized_name: str,
    row: dict[str, Any],
) -> RulesIndexJobPayload:
    """
    Build an index job payload from a Postgres CIR report row.

    Args:
        schedule: Product review routing metadata.
        ingredient: Original ingredient label from extraction.
        normalized_name: Normalized lookup key.
        row: Report metadata from ``_fetch_report_rows``.

    Returns:
        RulesIndexJobPayload: Job ready for enqueue.
    """
    ingredient_id = str(row.get("ingredient_id") or normalized_name)
    download_url = str(row.get("download_url") or "")
    return RulesIndexJobPayload(
        request_id=schedule.request_id,
        case_id=schedule.case_id,
        client_id=schedule.client_id,
        message_id=schedule.message_id,
        channel=schedule.channel,
        ingredient=ingredient,
        normalized_name=normalized_name,
        ingredient_id=ingredient_id,
        pdf_url=download_url,
        cir_ingredient_name=str(row.get("cir_ingredient_name") or ingredient),
        function=str(row.get("function") or "unknown"),
        report_url=str(row.get("report_url") or ""),
        attachment_url=str(row.get("attachment_url") or download_url),
        storage_path=str(row.get("storage_path") or download_url),
    )


def ingredient_index_cache_key(normalized_name: str) -> str:
    """
    Redis hash key marking a CIR ingredient as indexed for rules retrieval.

    Args:
        normalized_name: Normalized INCI lookup key.

    Returns:
        str: Cache key aligned with ``RulesEngine._cache_key``.
    """
    normalized = normalized_name.strip().lower()
    return f"{RULES_ENGINE_INGREDIENT_CACHE_PREFIX}:{normalized}"


def mark_ingredient_index_cache(
    redis_client: redis.Redis,
    *,
    normalized_name: str,
    ingredient_id: str,
) -> None:
    """
    Record successful indexing in the same Redis hash shape as ``RulesEngine``.

    Args:
        redis_client: Shared Redis client.
        normalized_name: Normalized ingredient key.
        ingredient_id: CIR ingredient identifier.
    """
    normalized = normalized_name.strip().lower()
    if not normalized:
        return

    def _mark() -> None:
        redis_client.hset(
            ingredient_index_cache_key(normalized),
            mapping={
                "normalized_name": normalized,
                "ingredient_id": ingredient_id,
                "indexed": "true",
            },
        )

    run_with_redis_retries(_mark, operation_name="rules_index_mark_cache")


def rules_index_batch_failed_key(request_id: str) -> str:
    """
    Redis SET key of normalized ingredients that failed indexing for one request.

    Args:
        request_id: Product review request id.

    Returns:
        str: Failure tracking key.
    """
    return f"{RULES_INDEX_BATCH_FAILED_KEY_PREFIX}{request_id.strip()}"


def register_index_batch_pending(
    redis_client: redis.Redis,
    request_id: str,
    normalized_name: str,
    *,
    ttl_sec: int | None = None,
) -> None:
    """
    Add a normalized ingredient to the pending batch SET for one request.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.
        normalized_name: Pending ingredient key.
        ttl_sec: Optional TTL refresh for the batch key.
    """
    batch_key = rules_index_batch_key(request_id)
    normalized = normalized_name.strip().lower()
    if not request_id.strip() or not normalized:
        return

    ttl = ttl_sec if ttl_sec is not None else RULES_INDEX_BATCH_TTL_SEC

    def _register() -> None:
        redis_client.sadd(batch_key, normalized)
        redis_client.expire(batch_key, ttl)

    run_with_redis_retries(_register, operation_name="rules_index_batch_sadd")


def rules_index_batch_key(request_id: str) -> str:
    """
    Redis SET key tracking pending ingredients for one product review request.

    Args:
        request_id: Product review request id.

    Returns:
        str: Batch coordination key.
    """
    return f"{RULES_INDEX_BATCH_KEY_PREFIX}{request_id.strip()}"


def stream_fields_from_job(payload: RulesIndexJobPayload) -> dict[str, str]:
    """
    Flatten a job payload into Redis stream field strings.

    Args:
        payload: Job to serialize.

    Returns:
        dict[str, str]: Fields suitable for ``XADD``.
    """
    body = payload.model_dump()
    return {
        "schema_version": str(body.get("schema_version") or "v1"),
        "request_id": str(payload.request_id),
        "case_id": str(payload.case_id),
        "client_id": str(payload.client_id),
        "normalized_name": str(payload.normalized_name),
        "payload": json.dumps(body, default=str),
    }


def parse_job_fields(fields: dict[str, Any]) -> RulesIndexJobPayload:
    """
    Parse stream message fields into a ``RulesIndexJobPayload``.

    Args:
        fields: Raw ``XREADGROUP`` field mapping.

    Returns:
        RulesIndexJobPayload: Parsed job.

    Raises:
        ValueError: When payload JSON is missing or invalid.
    """
    raw_payload = fields.get("payload")
    if isinstance(raw_payload, str) and raw_payload.strip():
        data = json.loads(raw_payload)
        if isinstance(data, dict):
            return RulesIndexJobPayload.model_validate(data)

    merged = {str(k): v for k, v in fields.items() if k != "payload"}
    if "payload" in fields and isinstance(fields["payload"], dict):
        merged.update(fields["payload"])
    return RulesIndexJobPayload.model_validate(merged)


def ensure_rules_index_consumer_group(redis_client: redis.Redis) -> None:
    """
    Create the rules index stream consumer group if it does not exist.

    Args:
        redis_client: Shared Redis client.

    Raises:
        redis.RedisError: When group creation fails for reasons other than BUSYGROUP.
    """

    def _create() -> None:
        redis_client.xgroup_create(
            RULES_INDEX_STREAM,
            RULES_INDEX_CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )

    try:
        run_with_redis_retries(_create, operation_name="rules_index_xgroup_create")
        logger.info(
            "rules index consumer group created: stream=%s group=%s",
            RULES_INDEX_STREAM,
            RULES_INDEX_CONSUMER_GROUP,
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def enqueue_rules_index_job(
    redis_client: redis.Redis,
    payload: RulesIndexJobPayload,
    *,
    dedupe_ttl_sec: int | None = None,
) -> EnqueueRulesIndexJobResult:
    """
    Enqueue one ingredient index job with per-ingredient dedupe.

    When dedupe SETNX fails, the job is not re-added to the stream (another worker
    or an in-flight message already owns indexing for that ingredient).

    Args:
        redis_client: Shared Redis client.
        payload: Job definition.
        dedupe_ttl_sec: Optional TTL override for the dedupe key.

    Returns:
        EnqueueRulesIndexJobResult: Whether a stream entry was created.
    """
    normalized = payload.normalized_name.strip().lower()
    if not normalized:
        raise ValueError("normalized_name is required to enqueue a rules index job")

    ttl = dedupe_ttl_sec if dedupe_ttl_sec is not None else RULES_INDEX_JOB_DEDUPE_TTL_SEC
    dedupe_key = rules_index_queued_key(normalized)

    def _dedupe() -> bool:
        return bool(redis_client.set(dedupe_key, payload.request_id, nx=True, ex=ttl))

    acquired = run_with_redis_retries(_dedupe, operation_name="rules_index_dedupe_setnx")
    if not acquired:
        logger.info(
            "rules index enqueue skipped (dedupe): normalized=%s request_id=%s",
            normalized,
            payload.request_id,
        )
        return EnqueueRulesIndexJobResult(enqueued=False, skipped_dedupe=True)

    fields = stream_fields_from_job(payload)

    def _xadd() -> str:
        return redis_client.xadd(
            RULES_INDEX_STREAM,
            fields,
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )

    msg_id = run_with_redis_retries(_xadd, operation_name="rules_index_xadd")
    msg_id_str = str(msg_id) if msg_id is not None else None
    logger.info(
        "rules index job enqueued: stream=%s msg_id=%s normalized=%s request_id=%s",
        RULES_INDEX_STREAM,
        msg_id_str,
        normalized,
        payload.request_id,
    )
    return EnqueueRulesIndexJobResult(enqueued=True, skipped_dedupe=False, message_id=msg_id_str)


def complete_index_batch_member(
    redis_client: redis.Redis,
    request_id: str,
    normalized_name: str,
) -> int:
    """
    Remove one ingredient from the pending batch SET and return remaining count.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.
        normalized_name: Completed ingredient key.

    Returns:
        int: ``SCARD`` after removal (0 means batch complete).
    """
    batch_key = rules_index_batch_key(request_id)
    normalized = normalized_name.strip().lower()

    def _complete() -> int:
        if normalized:
            redis_client.srem(batch_key, normalized)
        count = int(redis_client.scard(batch_key))
        if count <= 0:
            redis_client.delete(batch_key)
            return 0
        return count

    return int(run_with_redis_retries(_complete, operation_name="rules_index_batch_srem"))


def record_index_batch_failure(
    redis_client: redis.Redis,
    request_id: str,
    normalized_name: str,
) -> None:
    """
    Track a failed index attempt for batch outcome reporting.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.
        normalized_name: Ingredient that failed indexing.
    """
    normalized = normalized_name.strip().lower()
    if not request_id.strip() or not normalized:
        return
    failed_key = rules_index_batch_failed_key(request_id)

    def _record() -> None:
        redis_client.sadd(failed_key, normalized)
        redis_client.expire(failed_key, RULES_INDEX_BATCH_TTL_SEC)

    run_with_redis_retries(_record, operation_name="rules_index_batch_failed_sadd")


def try_enqueue_product_review_index_ready(
    redis_client: redis.Redis,
    job: RulesIndexJobPayload,
    *,
    index_outcome: str,
) -> bool:
    """
    Emit ``product_review_index_ready`` once when a request's index batch finishes.

    Args:
        redis_client: Shared Redis client.
        job: Last completed job (provides routing metadata).
        index_outcome: ``success``, ``partial``, or ``failed``.

    Returns:
        bool: True when a new signal was published.
    """
    idempotency_key = f"{RULES_INDEX_READY_SIGNAL_KEY_PREFIX}{job.request_id}"

    def _claim() -> bool:
        return bool(
            redis_client.set(idempotency_key, index_outcome, nx=True, ex=RULES_INDEX_READY_SIGNAL_TTL_SEC)
        )

    if not run_with_redis_retries(_claim, operation_name="rules_index_ready_setnx"):
        logger.info(
            "index ready signal skipped (already sent): request_id=%s",
            job.request_id,
        )
        return False

    signal_payload = {
        "index_outcome": index_outcome,
        "request_id": job.request_id,
        "case_id": job.case_id,
    }

    def _xadd() -> str:
        return redis_client.xadd(
            ORCHESTRATOR_SIGNAL_STREAM,
            {
                "signal_type": ORCHESTRATOR_SIGNAL_TYPE_INDEX_READY,
                "signal_id": uuid.uuid4().hex,
                "request_id": job.request_id,
                "case_id": job.case_id,
                "client_id": job.client_id,
                "message_id": job.message_id,
                "channel": job.channel,
                "source_event_id": "",
                "payload": json.dumps(signal_payload, default=str),
                "notes": "CIR indexing batch completed; run final product rules pass.",
            },
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )

    msg_id = run_with_redis_retries(_xadd, operation_name="rules_index_ready_xadd")
    logger.info(
        "index ready signal enqueued: stream=%s msg_id=%s request_id=%s outcome=%s",
        ORCHESTRATOR_SIGNAL_STREAM,
        msg_id,
        job.request_id,
        index_outcome,
    )

    failed_key = rules_index_batch_failed_key(job.request_id)
    run_with_redis_retries(lambda: redis_client.delete(failed_key), operation_name="rules_index_batch_failed_clear")
    return True


def finalize_index_batch_if_complete(
    redis_client: redis.Redis,
    job: RulesIndexJobPayload,
    *,
    job_succeeded: bool,
) -> bool:
    """
    Update batch state after one job finishes and maybe emit the index-ready signal.

    Args:
        redis_client: Shared Redis client.
        job: Completed index job payload.
        job_succeeded: False when indexing failed terminally.

    Returns:
        bool: True when ``product_review_index_ready`` was emitted.
    """
    if not job_succeeded:
        record_index_batch_failure(redis_client, job.request_id, job.normalized_name)

    remaining = complete_index_batch_member(redis_client, job.request_id, job.normalized_name)
    if remaining > 0:
        return False

    failed_key = rules_index_batch_failed_key(job.request_id)

    def _failed_count() -> int:
        return int(redis_client.scard(failed_key))

    failed_count = int(run_with_redis_retries(_failed_count, operation_name="rules_index_batch_failed_count"))
    outcome = "success" if failed_count == 0 else "partial"

    return try_enqueue_product_review_index_ready(redis_client, job, index_outcome=outcome)


def rules_index_context_key(request_id: str) -> str:
    """
    Redis key for a serialized product-rules snapshot used on the final pass.

    Args:
        request_id: Product review request id.

    Returns:
        str: Context storage key.
    """
    return f"{RULES_INDEX_CONTEXT_KEY_PREFIX}{request_id.strip()}"


def save_product_index_context(redis_client: redis.Redis, request_id: str, context: dict[str, Any]) -> None:
    """
    Persist product signal state until ``product_review_index_ready`` runs the final pass.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.
        context: JSON-serializable snapshot (rules request, fields, etc.).
    """
    payload = dict(context)
    payload.setdefault("enqueued_at_ms", int(time.time() * 1000))

    def _save() -> None:
        redis_client.set(
            rules_index_context_key(request_id),
            json.dumps(payload, default=str),
            ex=RULES_INDEX_CONTEXT_TTL_SEC,
        )

    run_with_redis_retries(_save, operation_name="rules_index_context_set")


def is_index_batch_timed_out(context: dict[str, Any]) -> bool:
    """
    Return True when defer context exceeded ``RULES_INDEX_BATCH_TIMEOUT_SEC``.

    Args:
        context: Stored product index context.

    Returns:
        bool: True if the batch watchdog considers the wait stale.
    """
    enqueued_raw = context.get("enqueued_at_ms")
    if enqueued_raw is None:
        return False
    try:
        enqueued_ms = int(enqueued_raw)
    except (TypeError, ValueError):
        return False
    elapsed_ms = int(time.time() * 1000) - enqueued_ms
    return elapsed_ms > RULES_INDEX_BATCH_TIMEOUT_SEC * 1000


def reconcile_orphan_index_batch(redis_client: redis.Redis, request_id: str) -> int:
    """
    Remove leftover pending batch members before the final rules pass.

    Orphans can remain when dedupe skipped enqueue but batch was registered.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.

    Returns:
        int: Number of cleared pending members.
    """
    batch_key = rules_index_batch_key(request_id)

    def _clear() -> int:
        members_raw = redis_client.smembers(batch_key)
        if not members_raw:
            return 0
        members = [str(item) for item in members_raw]
        for normalized in members:
            record_index_batch_failure(redis_client, request_id, normalized)
        redis_client.delete(batch_key)
        return len(members)

    cleared = int(run_with_redis_retries(_clear, operation_name="rules_index_batch_reconcile"))
    if cleared > 0:
        logger.warning(
            "reconciled orphan index batch members: request_id=%s cleared=%s",
            request_id,
            cleared,
        )
    return cleared


def load_product_index_context(redis_client: redis.Redis, request_id: str) -> dict[str, Any]:
    """
    Load a stored product index context snapshot.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.

    Returns:
        dict[str, Any]: Parsed context or empty dict when missing.
    """
    raw = run_with_redis_retries(
        lambda: redis_client.get(rules_index_context_key(request_id)),
        operation_name="rules_index_context_get",
    )
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def claim_product_final_report_sent(redis_client: redis.Redis, request_id: str) -> bool:
    """
    Idempotency guard for the final product safety report outbound message.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.

    Returns:
        bool: True when this caller should send the final report.
    """
    key = f"{PRODUCT_REPORT_SENT_KEY_PREFIX}{request_id.strip()}"

    def _claim() -> bool:
        return bool(redis_client.set(key, "1", nx=True, ex=RULES_INDEX_CONTEXT_TTL_SEC))

    return bool(run_with_redis_retries(_claim, operation_name="rules_product_report_sent_setnx"))


def claim_product_index_pending_sent(redis_client: redis.Redis, request_id: str) -> bool:
    """
    Idempotency guard for the interim “indexing in progress” outbound message.

    Args:
        redis_client: Shared Redis client.
        request_id: Product review request id.

    Returns:
        bool: True when this caller should send the interim message.
    """
    key = f"{PRODUCT_INDEX_PENDING_SENT_KEY_PREFIX}{request_id.strip()}"

    def _claim() -> bool:
        return bool(redis_client.set(key, "1", nx=True, ex=RULES_INDEX_CONTEXT_TTL_SEC))

    return bool(run_with_redis_retries(_claim, operation_name="rules_index_pending_sent_setnx"))


def index_results_require_async_wait(index_results: list[Any]) -> bool:
    """
    Return True when CIR indexing is still in flight for at least one ingredient.

    Args:
        index_results: Output from ``ensure_reports_exists``.

    Returns:
        bool: True when the product signal should defer the full rules report.
    """
    for item in index_results:
        source = getattr(item, "source", None) if not isinstance(item, dict) else item.get("source")
        if source == "queued":
            return True
    return False


def clear_rules_index_job_dedupe(redis_client: redis.Redis, normalized_name: str) -> None:
    """
    Clear dedupe state after index success or terminal failure so re-index can be scheduled.

    Args:
        redis_client: Shared Redis client.
        normalized_name: Normalized ingredient key.
    """

    key = rules_index_queued_key(normalized_name)

    def _delete() -> None:
        redis_client.delete(key)

    run_with_redis_retries(_delete, operation_name="rules_index_dedupe_clear")


__all__ = [
    "EnqueueRulesIndexJobResult",
    "ORCHESTRATOR_SIGNAL_TYPE_INDEX_READY",
    "RULES_INDEX_CONSUMER_GROUP",
    "RULES_INDEX_DLQ_STREAM",
    "RULES_INDEX_STREAM",
    "RulesIndexJobPayload",
    "RulesIndexScheduleContext",
    "build_index_job_payload",
    "build_product_index_pending_message",
    "clear_rules_index_job_dedupe",
    "complete_index_batch_member",
    "ensure_rules_index_consumer_group",
    "enqueue_rules_index_job",
    "claim_product_final_report_sent",
    "claim_product_index_pending_sent",
    "finalize_index_batch_if_complete",
    "index_results_require_async_wait",
    "is_index_batch_timed_out",
    "load_product_index_context",
    "reconcile_orphan_index_batch",
    "mark_ingredient_index_cache",
    "save_product_index_context",
    "parse_job_fields",
    "register_index_batch_pending",
    "try_enqueue_product_review_index_ready",
    "rules_index_batch_key",
    "rules_index_inline_enabled",
    "rules_index_queued_key",
    "stream_fields_from_job",
]

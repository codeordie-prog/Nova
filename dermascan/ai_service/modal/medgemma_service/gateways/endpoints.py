
"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-3-12
Licence: PolyForm Noncommercial License 1.0.0

Fast API Web endpoints for MedGemma service.
These endpoints are used to interact with the MedGemma service.
Through a distributed asynchronous processing system.

The async POST web endpoints receive requests and enqueue them to a Redis stream.
GET endpoints are used to check the status of the jobs.

The async workers process the jobs from the stream.
"""


from __future__ import annotations
 

import anyio
import base64
import binascii
import os
import time
import uuid
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
import redis

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

from dermascan.shared.logger import get_logger
from dermascan.shared.escalation_image import normalize_image_blob_path
from dermascan.shared.stream_pipeline_tracing import record_stream_produce
from dermascan.ai_service.modal.medgemma_service.schemas import (
    BatchEnqueueResponse,
    DiagnoseBase64Request,
    AuditImageRefRequest,
    EnqueueResponse,
    JobStatusResponse,
)
from dermascan.ai_service.modal.medgemma_service.gateways.queue_depth_manager import (
    _wait_for_queue_capacity,)

from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env

from dermascan.ai_service.modal.medgemma_service.gateways.custom_exceptions import (
    ImageValidationError,
    EmptyImageError,
    ImageTooLargeError,
)
from dermascan.ai_service.modal.medgemma_service.gateways.validation import (
    MAX_IMAGE_BYTES,
    _read_upload_limited,
    _validate_image_bytes,
)

from dermascan.ai_service.modal.medgemma_service.gateways.global_rate_limiter import (
    _enforce_global_rate_limit,
)

from dermascan.ai_service.modal.medgemma_service.worker import _ensure_consumer_group
from dermascan.billing.flags import billing_active, billing_enforce_enabled
from dermascan.billing.reservations import INSUFFICIENT_CREDITS_MESSAGE, InsufficientCreditsError

# Create FastAPI app
web_app = FastAPI(title="DermaScan MedGemma API", version="0.1")

# Initialize logger
logger = get_logger(__name__)

# get redis client
_redis: redis.Redis = redis_client_from_env().get_redis()

# job stream configuration - used for async job processing
_JOB_STREAM: str = os.environ.get("MEDGEMMA_JOB_STREAM", "medgemma:jobs")
_JOB_TTL_S: int = int(os.environ.get("MEDGEMMA_JOB_TTL_S", "86400"))
_MAX_QUEUE_LEN_HARD: int = int(os.environ.get("MAX_QUEUE_LEN_HARD", "350"))
_ENQUEUE_CAPACITY_RETRIES: int = int(os.environ.get("MEDGEMMA_ENQUEUE_CAPACITY_RETRIES", "5"))
_RETRY_AFTER_SECONDS: str = os.environ.get("MEDGEMMA_RETRY_AFTER_SECONDS", "1")
_CONSUMER_GROUP: str = os.environ.get("MEDGEMMA_CONSUMER_GROUP", "medgemma-workers")
_STREAM_MAXLEN: int = int(os.environ.get("MEDGEMMA_STREAM_MAXLEN", "5000"))
_RETRY_ON_RED_ATTEMPTS: int = int(os.environ.get("MEDGEMMA_RETRY_ON_RED_ATTEMPTS", "2"))
_RETRY_ON_RED_DELAY_MS: int = int(os.environ.get("MEDGEMMA_RETRY_ON_RED_DELAY_MS", "200"))
_ENQUEUE_RETRY_ATTEMPTS: int = int(os.environ.get("MEDGEMMA_ENQUEUE_RETRY_ATTEMPTS", "2"))
_ENQUEUE_RETRY_DELAY_MS: int = int(os.environ.get("MEDGEMMA_ENQUEUE_RETRY_DELAY_MS", "200"))


def _get_pending_count() -> int:
    """Return the number of pending (delivered, unacked) messages in the stream."""
    _ensure_consumer_group()
    summary = _redis.xpending(_JOB_STREAM, _CONSUMER_GROUP)
    if isinstance(summary, dict):
        return int(summary.get("pending", 0))
    return 0


async def _wait_for_capacity_with_retry() -> tuple[str, int]:
    """Retry queue pressure checks a few times before giving up."""
    state, depth = await _wait_for_queue_capacity()
    for _ in range(_RETRY_ON_RED_ATTEMPTS):
        if state != "red":
            break
        await anyio.sleep(_RETRY_ON_RED_DELAY_MS / 1000.0)
        state, depth = await _wait_for_queue_capacity()
    return state, depth


async def _enqueue_with_retry(func, *args):
    """Retry enqueue when capacity briefly exceeds thresholds."""
    for attempt in range(_ENQUEUE_RETRY_ATTEMPTS + 1):
        try:
            return await anyio.to_thread.run_sync(func, *args)
        except QueueCapacityExceededError:
            if attempt >= _ENQUEUE_RETRY_ATTEMPTS:
                raise
            await anyio.sleep(_ENQUEUE_RETRY_DELAY_MS / 1000.0)


class QueueCapacityExceededError(RuntimeError):
    """Raised when queue capacity is saturated at enqueue time."""


def _billing_stream_fields(
    *,
    job_id: str,
    channel: str,
    client_id: str,
    prompt: str,
    case_id: str = "",
) -> dict[str, str]:
    """
    Prepare billing hold metadata for a MedGemma enqueue.

    Args:
        job_id: Generated MedGemma job id.
        channel: Ingress channel.
        client_id: Messaging client id.
        prompt: Prompt/context for job-type inference.
        case_id: Optional case id.

    Returns:
        dict[str, str]: Redis fields to persist on the job hash/stream entry.

    Raises:
        InsufficientCreditsError: When enforce is on and the wallet is too low.
    """
    if not billing_active():
        return {}

    from dermascan.billing.medgemma import (
        billing_fields_for_stream,
        prepare_medgemma_job_billing,
    )

    try:
        context = prepare_medgemma_job_billing(
            job_id=job_id,
            channel=channel,
            client_id=client_id,
            prompt=prompt,
            case_id=case_id,
        )
        return billing_fields_for_stream(context)
    except InsufficientCreditsError:
        raise
    except Exception:
        logger.exception(
            "medgemma billing prepare failed at enqueue: job_id=%s channel=%s client_id=%s",
            job_id,
            channel,
            client_id,
        )
        if billing_enforce_enabled():
            raise
        return {}

# enqueue job function
def _enqueue_job(
    image_bytes: bytes,
    prompt: str,
    request_id: str = "",
    case_id: str = "",
    client_id: str = "",
    message_id: str = "",
    channel: str = "",
    group_id: str = "",
    group_expected_count: int = 0,
) -> str:
    """
    Enqueue a job to the Redis stream.
    This function uses Atomic enqueue gating principle.
    Atomic gating is needed here due to expected high concurrency and potential race conditions.
    We define a transaction with Multiple operations - including Enqueue Job to REDIS stream,
    Set job data, and Set job status.
    Atomic gating here means, we only enqueue under valid capacity or retry/fail
    
    Args:
        image_bytes: The image bytes to enqueue
        prompt: The prompt to use for the diagnosis
        
    Returns:
        str: The job ID
    """
    # Create a unique job ID
    job_id: str = uuid.uuid4().hex
    
    # Prepare the billing fields for the MedGemma job
    billing_fields: dict[str, str] = _billing_stream_fields(
        job_id=job_id,
        channel=channel,
        client_id=client_id,
        prompt=prompt,
        case_id=case_id,
    )

    # Encode the image to base64
    image_b64: str = base64.b64encode(image_bytes).decode("ascii")
    
    # Create a unique job key
    job_key: str = f"medgemma:job:{job_id}"
    
    # Get the current time
    now_s: int = int(time.time())

    # Bound retries to ENQUEUE_CAPACITY_RETRIES
    for _ in range(_ENQUEUE_CAPACITY_RETRIES):
        try:
            # Use a transaction pipeline to bundle together
            # the watch, check, and multi operations
            with _redis.pipeline(transaction=True) as pipe:

                # Watch the job stream to detect concurrent modifications
                pipe.watch(_JOB_STREAM)

                # Check the current pending depth (delivered but unacked)
                queue_len: int = _get_pending_count()

                # If the queue is at hard capacity, unwatch and raise an error
                if queue_len >= _MAX_QUEUE_LEN_HARD:
                    pipe.unwatch()
                    raise QueueCapacityExceededError("queue is at hard capacity")

                # Start a transaction to bundle the operations
                pipe.multi()
                pipe.hset(
                    job_key,
                    mapping={
                        "job_id": job_id,
                        "status": "queued",
                        "created_at": str(now_s),
                        **billing_fields,
                    },
                )
                pipe.expire(job_key, _JOB_TTL_S)
                pipe.xadd(
                    _JOB_STREAM,
                    {
                        "job_id": job_id,
                        "prompt": prompt,
                        "image_base64": image_b64,
                        "created_at": str(now_s),
                        "request_id": request_id,
                        "case_id": case_id,
                        "client_id": client_id,
                        "message_id": message_id,
                        "channel": channel,
                        "group_id": group_id,
                        "group_expected_count": str(group_expected_count),
                        **billing_fields,
                    },
                    maxlen=_STREAM_MAXLEN,
                    approximate=True,
                )
                results = pipe.execute()
                stream_msg_id = str(results[-1])
                record_stream_produce(
                    stream=_JOB_STREAM,
                    msg_id=stream_msg_id,
                    fields={
                        "job_id": job_id,
                        "request_id": request_id,
                        "case_id": case_id,
                        "client_id": client_id,
                        "message_id": message_id,
                        "channel": channel,
                        "group_id": group_id,
                    },
                    extra_metadata={"worker": "medgemma_gateway"},
                )
                return job_id
        except redis.WatchError:
            continue

    raise QueueCapacityExceededError("queue capacity contention: enqueue retries exhausted")


def _enqueue_ref_job(
    image_source_kind: str,
    image_source_value: str,
    prompt: str,
    mime_type: str = "",
    request_id: str = "",
    case_id: str = "",
    client_id: str = "",
    message_id: str = "",
    channel: str = "",
    group_id: str = "",
    group_expected_count: int = 0,
) -> str:
    """
    Enqueue an image-reference audit job to the Redis stream.

    Args:
        image_source_kind: Image reference type, for example "url" or "blob_ref".
        image_source_value: URL or blob reference to resolve before inference.
        prompt: The prompt to use for auditing.
        mime_type: Optional image MIME type.
        request_id: Request identifier for tracing.
        case_id: Case identifier for downstream routing.
        client_id: Client identifier for downstream routing.
        message_id: Source message identifier.
        channel: Source channel.
        group_id: Optional grouped-media identifier.
        group_expected_count: Expected grouped-media count.

    Returns:
        The enqueued MedGemma job ID.

    Raises:
        QueueCapacityExceededError: If queue capacity is saturated.
    """
    job_id: str = uuid.uuid4().hex
    billing_fields: dict[str, str] = _billing_stream_fields(
        job_id=job_id,
        channel=channel,
        client_id=client_id,
        prompt=prompt,
        case_id=case_id,
    )
    job_key: str = f"medgemma:job:{job_id}"
    now_s: int = int(time.time())

    for _ in range(_ENQUEUE_CAPACITY_RETRIES):
        try:
            with _redis.pipeline(transaction=True) as pipe:
                pipe.watch(_JOB_STREAM)
                queue_len: int = _get_pending_count()

                if queue_len >= _MAX_QUEUE_LEN_HARD:
                    pipe.unwatch()
                    raise QueueCapacityExceededError("queue is at hard capacity")

                pipe.multi()
                pipe.hset(
                    job_key,
                    mapping={
                        "job_id": job_id,
                        "status": "queued",
                        "created_at": str(now_s),
                        "image_source_kind": image_source_kind,
                        "image_source_value": image_source_value,
                        "mime_type": mime_type,
                        **billing_fields,
                    },
                )
                pipe.expire(job_key, _JOB_TTL_S)
                pipe.xadd(
                    _JOB_STREAM,
                    {
                        "job_id": job_id,
                        "prompt": prompt,
                        "image_source_kind": image_source_kind,
                        "image_source_value": image_source_value,
                        "mime_type": mime_type,
                        "created_at": str(now_s),
                        "request_id": request_id,
                        "case_id": case_id,
                        "client_id": client_id,
                        "message_id": message_id,
                        "channel": channel,
                        "group_id": group_id,
                        "group_expected_count": str(group_expected_count),
                        **billing_fields,
                    },
                    maxlen=_STREAM_MAXLEN,
                    approximate=True,
                )
                results = pipe.execute()
                stream_msg_id = str(results[-1])
                record_stream_produce(
                    stream=_JOB_STREAM,
                    msg_id=stream_msg_id,
                    fields={
                        "job_id": job_id,
                        "request_id": request_id,
                        "case_id": case_id,
                        "client_id": client_id,
                        "message_id": message_id,
                        "channel": channel,
                        "group_id": group_id,
                    },
                    extra_metadata={"worker": "medgemma_gateway", "flow": "image_ref"},
                )
                return job_id
        except redis.WatchError:
            continue

    raise QueueCapacityExceededError("queue capacity contention: ref enqueue retries exhausted")


def _enqueue_jobs_batch(jobs: list[tuple[bytes, str]]) -> list[str]:
    """
    Enqueue multiple jobs to the Redis stream.
    
    Args:
        jobs: List of tuples containing (image_bytes, prompt)
        
    Returns:
        list[str]: List of job IDs
    """
    if not jobs:
        return []

    now_s: str = str(int(time.time()))
    job_ids: list[str] = []

    encoded_jobs: list[tuple[str, str, str]] = []
    for image_bytes, prompt in jobs:
        job_id: str = uuid.uuid4().hex
        job_key: str = f"medgemma:job:{job_id}"
        image_b64: str = base64.b64encode(image_bytes).decode("ascii")
        job_ids.append(job_id)
        encoded_jobs.append((job_id, job_key, image_b64))

    required_slots: int = len(encoded_jobs)

    for _ in range(_ENQUEUE_CAPACITY_RETRIES):
        try:
            with _redis.pipeline(transaction=True) as pipe:
                pipe.watch(_JOB_STREAM)
                queue_len: int = _get_pending_count()
                if queue_len + required_slots > _MAX_QUEUE_LEN_HARD:
                    pipe.unwatch()
                    raise QueueCapacityExceededError("queue is at hard capacity")

                pipe.multi()
                for (job_id, job_key, image_b64), (_, prompt) in zip(encoded_jobs, jobs, strict=True):
                    pipe.hset(
                        job_key,
                        mapping={
                            "job_id": job_id,
                            "status": "queued",
                            "created_at": now_s,
                        },
                    )
                    pipe.expire(job_key, _JOB_TTL_S)
                    pipe.xadd(
                        _JOB_STREAM,
                        {
                            "job_id": job_id,
                            "prompt": prompt,
                            "image_base64": image_b64,
                            "created_at": now_s,
                        },
                        maxlen=_STREAM_MAXLEN,
                        approximate=True,
                    )
                results = pipe.execute()
                for idx, (job_id, _, _) in enumerate(encoded_jobs):
                    stream_msg_id = str(results[idx * 3 + 2])
                    record_stream_produce(
                        stream=_JOB_STREAM,
                        msg_id=stream_msg_id,
                        fields={"job_id": job_id},
                        extra_metadata={"worker": "medgemma_gateway", "flow": "batch"},
                    )
                return job_ids
        except redis.WatchError:
            continue

    raise QueueCapacityExceededError("queue capacity contention: batch enqueue retries exhausted")


def _get_job_data(job_id: str) -> dict[str, str]:
    """
    Get job data from Redis.
    
    Args:
        job_id: The job ID
        
    Returns:
        dict[str, str]: The job data
    """
    return _redis.hgetall(f"medgemma:job:{job_id}")


# Diagnose endpoint
@web_app.post("/diagnose", response_model=EnqueueResponse)
async def diagnose(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
) -> EnqueueResponse:
    """
    Diagnose an image using MedGemma.
    
    Args:
        image: The image to diagnose
        prompt: The prompt to use for the diagnosis
        
    Returns:
        dict: The diagnosis result
    """
    logger.info("Diagnose endpoint called")

    try:
        await _enforce_global_rate_limit(cost=1)

        if not prompt.strip():
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        if image.content_type is not None and not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Unsupported content type: {image.content_type}")

        # Read and validate image
        image_bytes: bytes = await _read_upload_limited(image, max_bytes=MAX_IMAGE_BYTES)
        _validate_image_bytes(image_bytes)

        # Check queue capacity with retry
        state, _ = await _wait_for_capacity_with_retry()

        if state == "red":
            raise HTTPException(
                status_code=429,
                detail="Service is overloaded. Please try again later.",
                headers={"Retry-After": _RETRY_AFTER_SECONDS},
            )

        # Enqueue job with retry on contention
        job_id: str = await _enqueue_with_retry(_enqueue_job, image_bytes, prompt)

        return EnqueueResponse(job_id=job_id, status="queued")


    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except QueueCapacityExceededError:
        raise HTTPException(
            status_code=429,
            detail="Service is overloaded. Please try again later.",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )

    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")
        
    except Exception:
        logger.exception("Unhandled error in /diagnose")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        try:
            await image.close()
        except Exception:
            logger.debug("Failed to close upload")


@web_app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    data: dict[str, str] = await anyio.to_thread.run_sync(_get_job_data, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=job_id,
        status=data.get("status", "unknown"),
        boot_id=data.get("boot_id"),
        text=data.get("text"),
        confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
        error=data.get("error"),
    )

# Diagnose batch endpoint
@web_app.post("/diagnose_batch", response_model=BatchEnqueueResponse)
async def diagnose_batch(
    request: Request,
    images: list[UploadFile] = File(...),
    prompts: list[str] = Form(...),
) -> BatchEnqueueResponse:
    """
    Diagnose multiple images using MedGemma.
    
    Args:
        images: The images to diagnose
        prompts: The prompts to use for the diagnosis
        
    Returns:
        dict: The diagnosis results
    """
    logger.info("Diagnose batch endpoint called")

    try:
        await _enforce_global_rate_limit(cost=len(images))

        if len(images) != len(prompts):
            raise HTTPException(
                status_code=400,
                detail=f"images and prompts must have the same length: {len(images)} != {len(prompts)}",
            )

        if any(not p.strip() for p in prompts):
            raise HTTPException(status_code=400, detail="All prompts must be non-empty")

        enqueue_jobs: list[tuple[bytes, str]] = []
        for img, p in zip(images, prompts, strict=True):
            if img.content_type is not None and not img.content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail=f"Unsupported content type: {img.content_type}")

            img_bytes: bytes = await _read_upload_limited(img, max_bytes=MAX_IMAGE_BYTES)
            _validate_image_bytes(img_bytes)
            enqueue_jobs.append((img_bytes, p))

        # Check queue capacity with retry
        state, _ = await _wait_for_capacity_with_retry()

        if state == "red":
            raise HTTPException(
                status_code=429,
                detail="Service is overloaded. Please try again later.",
                headers={"Retry-After": _RETRY_AFTER_SECONDS},
            )

        job_ids: list[str] = await _enqueue_with_retry(_enqueue_jobs_batch, enqueue_jobs)

        return BatchEnqueueResponse(job_ids=job_ids, status="queued")
        
    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except QueueCapacityExceededError:
        raise HTTPException(
            status_code=429,
            detail="Service is overloaded. Please try again later.",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )

    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    except Exception:
        logger.exception("Unhandled error in /diagnose_batch")
        raise HTTPException(status_code=500, detail="Internal server error")

    finally:
        for img in images:
            try:
                await img.close()
            except Exception:
                logger.debug("Failed to close upload")


# Diagnose base64 endpoint
@web_app.post("/diagnose_base64", response_model=EnqueueResponse)
async def diagnose_base64(
    request: Request,
    payload: DiagnoseBase64Request = Body(...),
) -> EnqueueResponse:
    """
    Diagnose an image using MedGemma.
    
    Args:
        image_base64: The base64 encoded image
        prompt: The prompt to use for the diagnosis
        
    Returns:
        dict: The diagnosis result
    """
    logger.info("diagnose_base64 endpoint called")

    await _enforce_global_rate_limit(cost=1)
    try:
        image_bytes: bytes = base64.b64decode(payload.image_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        logger.error(f"Invalid base64: {e}")
        raise HTTPException(status_code=400, detail="Invalid base64")

    try:
        if not payload.prompt.strip():
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        _validate_image_bytes(image_bytes)

        # Check queue capacity with retry
        state, _ = await _wait_for_capacity_with_retry()

        if state == "red":
            raise HTTPException(
                status_code=429,
                detail="Service is overloaded. Please try again later.",
                headers={"Retry-After": _RETRY_AFTER_SECONDS},
            )

        job_id: str = await _enqueue_with_retry(
            _enqueue_job,
            image_bytes,
            payload.prompt,
            str(payload.request_id or ""),
            str(payload.case_id or payload.request_id or ""),
            str(payload.client_id or ""),
            str(payload.message_id or payload.request_id or ""),
            str(payload.channel or ""),
            str(payload.group_id or ""),
            int(payload.group_expected_count or 0),
        )
        return EnqueueResponse(job_id=job_id, status="queued")

    except HTTPException:
        raise

    except (ImageValidationError, EmptyImageError, ImageTooLargeError) as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except InsufficientCreditsError:
        raise HTTPException(status_code=402, detail=INSUFFICIENT_CREDITS_MESSAGE)

    except QueueCapacityExceededError:
        raise HTTPException(
            status_code=429,
            detail="Service is overloaded. Please try again later.",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )

    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    except Exception:
        logger.exception("Unhandled error in /diagnose_base64")
        raise HTTPException(status_code=500, detail="Internal server error")


@web_app.post("/audit_image_ref", response_model=EnqueueResponse)
@web_app.post("/audit_ref", response_model=EnqueueResponse)
async def audit_image_ref(
    request: Request,
    payload: AuditImageRefRequest = Body(...),
) -> EnqueueResponse:
    """
    Enqueue a image audit job using an image URL and lightweight context.

    Args:
        request: The FastAPI request.
        payload: Ref-based diagnosis payload.

    Returns:
        The MedGemma enqueue response.

    Raises:
        HTTPException: If validation, queue pressure, or Redis access fails.
    """
    logger.info("audit_image_ref endpoint called")

    try:
        await _enforce_global_rate_limit(cost=1)

        context: str = payload.context.strip()
        mime_type: str = str(payload.mime_type or "image/jpeg").strip()
        image_blob_path: str = normalize_image_blob_path(str(payload.image_blob_path or "").strip())
        image_url: str = str(payload.image_url or "").strip()

        if not context:
            raise HTTPException(status_code=400, detail="Audit context cannot be empty")

        if image_blob_path:
            image_source_kind = "blob_path"
            image_source_value = image_blob_path
        elif image_url:
            if not image_url.lower().startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="Image URL must use http or https")
            image_source_kind = "url"
            image_source_value = image_url
        else:
            raise HTTPException(status_code=400, detail="image_blob_path or image_url is required")

        state, _ = await _wait_for_capacity_with_retry()
        if state == "red":
            raise HTTPException(
                status_code=429,
                detail="Service is overloaded. Please try again later.",
                headers={"Retry-After": _RETRY_AFTER_SECONDS},
            )

        job_id: str = await _enqueue_with_retry(
            _enqueue_ref_job,
            image_source_kind,
            image_source_value,
            context,
            mime_type,
            payload.request_id,
            str(payload.case_id or payload.request_id),
            str(payload.client_id or ""),
            str(payload.message_id or payload.request_id),
            str(payload.channel or ""),
            str(payload.group_id or ""),
            int(payload.group_expected_count or 0),
        )
        return EnqueueResponse(job_id=job_id, status="queued")

    except HTTPException:
        raise

    except InsufficientCreditsError:
        raise HTTPException(status_code=402, detail=INSUFFICIENT_CREDITS_MESSAGE)

    except QueueCapacityExceededError:
        raise HTTPException(
            status_code=429,
            detail="Service is overloaded. Please try again later.",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )

    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    except Exception:
        logger.exception("Unhandled error in /audit_image_ref")
        raise HTTPException(status_code=500, detail="Internal server error")




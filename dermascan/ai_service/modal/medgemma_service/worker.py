"""
Author: Dermascan Team
Date: 2026-3-12
License: MIT

MedGemma service worker.
This worker processes jobs from a Redis stream.
The Stream is created by the FAST API POST endpoints.
"""

import base64
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import os
import socket
import time
from typing import Any

from redis import Redis

from dermascan.ai_service.modal.medgemma_service.app import MedGemmaService, app as modal_app
from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env
from dermascan.shared.logger import get_logger

_redis = redis_client_from_env().get_redis()

logger = get_logger(__name__)

_JOB_STREAM: str = os.environ.get("MEDGEMMA_JOB_STREAM", "medgemma:jobs")

_JOB_TTL_S: int = int(os.environ.get("MEDGEMMA_JOB_TTL_S", "86400"))
_MAX_ATTEMPTS: int = int(os.environ.get("MEDGEMMA_MAX_ATTEMPTS", "3"))
_BATCH_SIZE: int = int(os.environ.get("MEDGEMMA_STREAM_BATCH_SIZE", "10"))
_BLOCK_MS: int = int(os.environ.get("MEDGEMMA_STREAM_BLOCK_MS", "500"))
_MAX_INFLIGHT: int = max(1, int(os.environ.get("MEDGEMMA_MAX_INFLIGHT", "4")))
_CONSUMER_GROUP: str = os.environ.get("MEDGEMMA_CONSUMER_GROUP", "medgemma-workers")
_CONSUMER_NAME: str = os.environ.get("MEDGEMMA_CONSUMER_NAME", socket.gethostname())


def _job_key(job_id: str) -> str:
    """
    Generate a Redis key for a job.
    
    Args:
        job_id: The job ID
        
    Returns:
        The Redis key for the job
    """
    return f"medgemma:job:{job_id}"


def _mark_status(job_id: str, *, status: str, **fields: str) -> None:
    """
    Update the status of a job in Redis.
    
    Args:
        job_id: The job ID
        status: The new status
        **fields: Additional fields to update
    """
    # Get the Redis key for the job
    key: str = _job_key(job_id)

    # Create a mapping of fields to update
    mapping: dict[str, str] = {"job_id": job_id, "status": status, **{k: str(v) for k, v in fields.items()}}
    
    # Execute the pipeline
    pipe: Redis.pipeline = _redis.pipeline(transaction=False)
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, _JOB_TTL_S)
    pipe.execute()
    logger.info("job status updated: job_id=%s status=%s fields=%s", job_id, status, list(fields.keys()))


def _start_attempt(job_id: str) -> int:
    """
    Increment attempts and mark job as running in one Redis pipeline.
    
    Args:
        job_id: The job ID
        
    Returns:
        The new attempt count.
    """
    key: str = _job_key(job_id)
    started_at: str = str(int(time.time()))
    pipe: Redis.pipeline = _redis.pipeline(transaction=False)
    pipe.hincrby(key, "attempts", 1)

    # Set job as running
    pipe.hset(
        key,
        mapping={
            "job_id": job_id,
            "status": "running",
            "started_at": started_at,
        },
    )
    
    # Set expiration
    pipe.expire(key, _JOB_TTL_S)
    
    # Execute the pipeline
    result = pipe.execute()
    attempts: int = int(result[0])
    logger.info("attempt incremented and running set: job_id=%s attempts=%s", job_id, attempts)
    return attempts


def _ensure_consumer_group() -> None:
    """
    A consumer group ensures that messages are processed by a maximum of one consumer.
    This ensures that each job is processed by only one worker at a time.
    
    Ensure the consumer group exists for the job stream.
    
    Raises:
        Exception: If the consumer group creation fails
    """
    try:

        # Create the consumer group if it doesn't exist
        _redis.xgroup_create(_JOB_STREAM, _CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("consumer group created: stream=%s group=%s", _JOB_STREAM, _CONSUMER_GROUP)

    except Exception as e:
        if "BUSYGROUP" in str(e):
            logger.info("consumer group exists: stream=%s group=%s", _JOB_STREAM, _CONSUMER_GROUP)
            return
        raise


def _dispatch_message(msg_id: str, payload: dict[str, Any], service: MedGemmaService) -> tuple[str, str, Any] | None:
    """
    Validate and dispatch a message to Modal using spawn.
    
    Args:
        msg_id: The message ID
        payload: The message payload
        service: The MedGemma service instance

    Returns:
        A tuple containing (job_id, msg_id, call_handle) on successful dispatch,
        otherwise None.
    """
    logger.info("dispatch received: msg_id=%s keys=%s", msg_id, list(payload.keys()))
    job_id: str | None = payload.get("job_id")

    if not job_id:
        logger.warning("skipping message missing job_id: msg_id=%s", msg_id)

        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        return

    attempts: int = _start_attempt(job_id)

    if attempts > _MAX_ATTEMPTS:
        logger.warning("max attempts exceeded: job_id=%s attempts=%s", job_id, attempts)

        # Mark job as failed and acknowledge the message
        _mark_status(job_id, status="failed", error="max attempts exceeded")

        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        return None

    # Extract prompt and image from payload
    prompt: str = payload.get("prompt") or ""
    image_b64: str = payload.get("image_base64") or ""

    # Validate image
    if not image_b64:
        logger.warning("missing image_base64: job_id=%s", job_id)

        # Update job status to failed
        _mark_status(job_id, status="failed", error="missing image_base64", finished_at=str(int(time.time())))
        
        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        return None

    try:
        image_bytes: bytes = base64.b64decode(image_b64.encode("ascii"), validate=True)
        logger.info("spawn dispatch starting: job_id=%s bytes=%s", job_id, len(image_bytes))
        call_handle = service.run_one.spawn(image_bytes=image_bytes, prompt=prompt)
        logger.info("spawn dispatch done: job_id=%s", job_id)
        return job_id, msg_id, call_handle
    except Exception as e:
        logger.exception("dispatch failed: job_id=%s", job_id)
        _mark_status(job_id, status="failed", error=str(e), finished_at=str(int(time.time())))
        
        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        logger.info("message acked after dispatch failure: msg_id=%s", msg_id)
        return None


def _collect_result(job_id: str, msg_id: str, call_handle: Any) -> None:
    """
    Wait for a spawned Modal call and persist the final result.

    Args:
        job_id: The job ID
        msg_id: The Redis message ID
        call_handle: Modal spawn handle
    """
    try:
        boot_id, text = call_handle.get()
        logger.info("inference done: job_id=%s boot_id=%s text_len=%s", job_id, boot_id, len(text) if text is not None else 0)
        _mark_status(
            job_id,
            status="done",
            boot_id=boot_id,
            text=text,
            finished_at=str(int(time.time())),
        )
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        logger.info("message acked: msg_id=%s", msg_id)
    except Exception as e:
        logger.exception("inference failed: job_id=%s", job_id)
        _mark_status(job_id, status="failed", error=str(e), finished_at=str(int(time.time())))
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        logger.info("message acked after failure: msg_id=%s", msg_id)


def _drain_completed(inflight: set[Future[None]], *, timeout: float) -> None:
    """
    Drain completed futures from the inflight set.

    Args:
        inflight: Active in-flight collector futures.
        timeout: Wait timeout in seconds.
    """
    if not inflight:
        return

    done, _ = wait(inflight, timeout=timeout, return_when=FIRST_COMPLETED)
    for future in done:
        inflight.remove(future)
        try:
            future.result()
        except Exception:
            logger.exception("unexpected collector task failure")


def run_worker_forever() -> None:
    """
    Run the worker in an infinite loop, processing jobs from the Redis stream.
    
    This function:
    - Connects to Redis
    - Ensures the consumer group exists
    - Starts the Modal app context
    - Processes jobs from the Redis stream in a loop
    
    """
    logger.info("redis ping...")

    try:
        # Ping redis to ensure it's connected
        _redis.ping()

    except Exception as e:
        logger.error("redis ping failed: %s", e)
        raise

    logger.info("redis ping ok")

    # Ensure consumer group exists
    _ensure_consumer_group()
    logger.info("consumer group ensured")

    logger.info("starting modal app context")

    # Start Modal app context
    with modal_app.run():
        service = MedGemmaService()
        logger.info(
            "worker started: stream=%s group=%s consumer=%s batch=%s block_ms=%s max_inflight=%s",
            _JOB_STREAM,
            _CONSUMER_GROUP,
            _CONSUMER_NAME,
            _BATCH_SIZE,
            _BLOCK_MS,
            _MAX_INFLIGHT,
        )
        
        # Create thread pool for processing jobs
        with ThreadPoolExecutor(max_workers=_MAX_INFLIGHT) as pool:
            inflight: set[Future[None]] = set()
            while True:
                _drain_completed(inflight, timeout=0.0)

                available_capacity: int = _MAX_INFLIGHT - len(inflight)
                if available_capacity <= 0:
                    _drain_completed(inflight, timeout=1.0)
                    continue

                resp: list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]] = _redis.xreadgroup(
                    groupname=_CONSUMER_GROUP,
                    consumername=_CONSUMER_NAME,
                    streams={_JOB_STREAM: ">"},
                    count=min(_BATCH_SIZE, available_capacity),
                    block=_BLOCK_MS,
                )

                # Check if there are any messages
                if not resp:
                    logger.debug("no messages available")

                    # If no messages are available, continue to the next iteration
                    continue
                
                # Process messages
                for _, messages in resp:
                    for msg_id, payload in messages:
                        msg_id_str = msg_id.decode("utf-8") if isinstance(msg_id, (bytes, bytearray)) else str(msg_id)
                        dispatched = _dispatch_message(msg_id_str, payload, service)
                        if dispatched is None:
                            continue
                        job_id, ack_msg_id, call_handle = dispatched
                        inflight.add(pool.submit(_collect_result, job_id, ack_msg_id, call_handle))


if __name__ == "__main__":
    run_worker_forever()



"""
Author: Dermascan Team
Date: 2026-3-12
License: MIT

MedGemma service worker.
This worker processes jobs from a Redis stream.
The Stream is created by the FAST API POST endpoints.

Worker utilizes Microbatching to dispatch jobs to the MedGemma model.
This is implemented inorder to achieve a near one forward pass behavior.
"""

import base64
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import os
import socket
import threading
import time
from typing import Any

from redis import Redis

from dermascan.ai_service.modal.medgemma_service.app import MedGemmaService, app as modal_app
from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env
from dermascan.ai_service.postprocessing.escalation import route_case_after_inference
from dermascan.shared.logger import get_logger
from dermascan.backend.services.agents import reclaim_logic

_redis = redis_client_from_env().get_redis()

# Initialize reclaim_logic with the Redis client
reclaim_logic.set_redis_client(_redis)

logger = get_logger(__name__)

_JOB_STREAM: str = os.environ.get("MEDGEMMA_JOB_STREAM", "medgemma:jobs")
_JOB_TTL_S: int = int(os.environ.get("MEDGEMMA_JOB_TTL_S", "86400"))
_MAX_ATTEMPTS: int = int(os.environ.get("MEDGEMMA_MAX_ATTEMPTS", "3"))
_BATCH_SIZE: int = int(os.environ.get("MEDGEMMA_STREAM_BATCH_SIZE", "10"))
_BATCH_WINDOW_S: int = float(os.environ.get("MEDGEMMA_BATCH_WINDOW_S", "2.0"))
_BLOCK_MS: int = int(os.environ.get("MEDGEMMA_STREAM_BLOCK_MS", "500"))
_MAX_INFLIGHT: int = max(1, int(os.environ.get("MEDGEMMA_MAX_INFLIGHT", "12")))
_CONSUMER_GROUP: str = os.environ.get("MEDGEMMA_CONSUMER_GROUP", "medgemma-workers")
_CONSUMER_NAME_BASE: str = os.environ.get("MEDGEMMA_CONSUMER_NAME", "medgemma-worker")
_CONSUMER_NAME: str = f"{_CONSUMER_NAME_BASE}-{socket.gethostname()}-{os.getpid()}"
_MEDGEMMA_DLQ_STREAM: str = os.environ.get("MEDGEMMA_DLQ_STREAM", "medgemma:dlq")

# RPC retry configuration
MEDGEMMA_RPC_MAX_RETRIES: int = 3
MEDGEMMA_RPC_RETRY_DELAY_MS: int = 200
MEDGEMMA_RPC_RETRY_BACKOFF_MULTIPLIER: float = 2.0


def _dispatch_with_retry(dispatch_func, *args, **kwargs) -> Any:
    """
    Dispatch RPC call with retry logic for transient errors.

    Args:
        dispatch_func: The RPC function to call
        *args: Positional arguments for the RPC function
        **kwargs: Keyword arguments for the RPC function

    Returns:
        The result of the RPC call

    Raises:
        Exception: If all retries are exhausted
    """
    last_exception: Exception | None = None

    for attempt in range(MEDGEMMA_RPC_MAX_RETRIES + 1):
        try:
            return dispatch_func(*args, **kwargs)
        except Exception as exc:
            last_exception = exc

            # Check if error is retryable (network, timeout, transient)
            error_str = str(exc).lower()
            is_retryable = any(
                keyword in error_str
                for keyword in ["timeout", "connection", "network", "transient", "unavailable"]
            )

            if not is_retryable or attempt >= MEDGEMMA_RPC_MAX_RETRIES:
                raise

            # Calculate delay with exponential backoff
            delay_ms = MEDGEMMA_RPC_RETRY_DELAY_MS * (
                MEDGEMMA_RPC_RETRY_BACKOFF_MULTIPLIER ** attempt
            )

            logger.warning(
                "rpc dispatch failed (retryable): attempt=%s/%s delay_ms=%s error=%s",
                attempt + 1,
                MEDGEMMA_RPC_MAX_RETRIES + 1,
                delay_ms,
                str(exc),
            )
            time.sleep(delay_ms / 1000.0)

    # This should never be reached, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected error in RPC retry logic")


@dataclass
class _JobEntry:
    """
    One Job collected during batch window
    """
    job_id: str
    prompt: str
    image_bytes: bytes
    msg_id: str
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    group_id: str
    group_expected_count: int = 0


def _to_str(value: Any) -> str:
    """
    Convert a value to a string, handling bytes and None.
    
    Args:
        value: The value to convert
        
    Returns:
        The string representation of the value
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return "" if value is None else str(value)


def _to_float(value: Any) -> float | None:
    """
    Convert a value to a float, handling None.
    
    Args:
        value: The value to convert
        
    Returns:
        The float representation of the value, or None if conversion fails
    """
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


# Generate a Redis key for a job
def _job_key(job_id: str) -> str:
    """
    Generate a Redis key for a job.
    
    Args:
        job_id: The job ID
        
    Returns:
        The Redis key for the job
    """
    return f"medgemma:job:{job_id}"

# Update the status of a job in Redis
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

# Increment attempts and mark job as running in one Redis pipeline
# a REDIS pipeline is a way to group multiple commands together and execute them in a single round trip to the server
def _start_attempt(job_id: str) -> int:
    """
    Increment attempts and mark job as running in one Redis pipeline.
    The Pipeline executes all commands in a single round trip to the server
    
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

# Ensure a consumer group exists prior to consuming jobs
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
        _redis.xdel(_JOB_STREAM, msg_id)
        return

    attempts: int = _start_attempt(job_id)

    if attempts > _MAX_ATTEMPTS:
        logger.warning("max attempts exceeded: job_id=%s attempts=%s", job_id, attempts)

        # Mark job as failed and acknowledge the message
        _mark_status(job_id, status="failed", error="max attempts exceeded")

        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        _redis.xdel(_JOB_STREAM, msg_id)
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
        _redis.xdel(_JOB_STREAM, msg_id)
        return None

    try:
        image_bytes: bytes = base64.b64decode(image_b64.encode("ascii"), validate=True)
        logger.info("spawn dispatch starting: job_id=%s bytes=%s", job_id, len(image_bytes))

        # TODO : should this call_handle use a batched dispatch logic?
        call_handle = _dispatch_with_retry(service.run_one.spawn, image_bytes=image_bytes, prompt=prompt)
        logger.info("spawn dispatch done: job_id=%s", job_id)
        return job_id, msg_id, call_handle
    except Exception as e:
        logger.exception("dispatch failed: job_id=%s", job_id)
        _mark_status(job_id, status="failed", error=str(e), finished_at=str(int(time.time())))
        
        # Acknowledge the message to remove it from the stream
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        _redis.xdel(_JOB_STREAM, msg_id)
        logger.info("message acked after dispatch failure: msg_id=%s", msg_id)
        return None

def _collect_batch() -> list[_JobEntry]:
    """
    Collect results untill batch is full OR time window expires
    
    Returns:
        List of job entries with results
    """
    batch: list[_JobEntry] = []
    deadline: float = time.monotonic() + _BATCH_WINDOW_S

    while len(batch) < _BATCH_SIZE:

        # Get the remaining time in the batch window
        remaining_ms: int = max(0, int((deadline - time.monotonic()) * 1000))
        
        # if window has expired
        if remaining_ms == 0:
            break

        # Consume jobs from the stream
        resp = _redis.xreadgroup(
            groupname=_CONSUMER_GROUP,
            consumername=_CONSUMER_NAME,
            streams={_JOB_STREAM: ">"},
            count=_BATCH_SIZE - len(batch),
            block=remaining_ms
        )

        if not resp:
            break  # Nothing has arrived yet

        for _, messages in resp:
            for msg_id, payload in messages:
                msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                job_id_raw: Any = payload.get("job_id") or payload.get(b"job_id")
                job_id: str | None
                if isinstance(job_id_raw, (bytes, bytearray)):
                    job_id = job_id_raw.decode("utf-8", errors="ignore")
                elif job_id_raw is None:
                    job_id = None
                else:
                    job_id = str(job_id_raw)

                if not job_id:
                    # Malformed message
                    _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id_str)
                    _redis.xdel(_JOB_STREAM, msg_id_str)
                    continue

                image_b64_raw: Any = payload.get("image_base64")
                if image_b64_raw is None:
                    image_b64_raw = payload.get(b"image_base64")

                image_base64: str
                if isinstance(image_b64_raw, (bytes, bytearray)):
                    image_base64 = image_b64_raw.decode("utf-8", errors="ignore")
                elif image_b64_raw is None:
                    image_base64 = ""
                else:
                    image_base64 = str(image_b64_raw)

                if not image_base64:
                    # Malformed message
                    _mark_status(job_id, status="failed", error="missing image_base64", finished_at=str(int(time.time())))
                    _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id_str)
                    _redis.xdel(_JOB_STREAM, msg_id_str)
                    continue
                
                try:
                    image_bytes = base64.b64decode(image_base64.encode("ascii"), validate=True)
                    
                except Exception as e:
                    _mark_status(job_id, status = "failed", error = f"Failed to decode image_base64: {e}")
                    logger.error("Failed to decode image_base64: %s", e)
                    _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id_str)
                    _redis.xdel(_JOB_STREAM, msg_id_str)
                    continue

                attempts: int = _start_attempt(job_id)
                if attempts > _MAX_ATTEMPTS:
                    _mark_status(job_id, status="failed", error="Max attempts reached")
                    _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id_str)
                    _redis.xdel(_JOB_STREAM, msg_id_str)
                    continue

                batch.append(
                    _JobEntry(
                        job_id=job_id,
                        msg_id=msg_id_str,
                        image_bytes=image_bytes,
                        request_id=_to_str(payload.get("request_id") or payload.get(b"request_id")),
                        case_id=_to_str(payload.get("case_id") or payload.get(b"case_id")),
                        client_id=_to_str(payload.get("client_id") or payload.get(b"client_id")),
                        message_id=_to_str(payload.get("message_id") or payload.get(b"message_id")),
                        channel=_to_str(payload.get("channel") or payload.get(b"channel")),
                        group_id=_to_str(payload.get("group_id") or payload.get(b"group_id")),
                        group_expected_count=int(
                            _to_str(payload.get("group_expected_count") or payload.get(b"group_expected_count") or "0")
                            or "0"
                        ),
                        prompt=(
                            payload.get("prompt")
                            or payload.get(b"prompt")
                            or ""
                        ).decode("utf-8", errors="ignore")
                        if isinstance(payload.get("prompt") or payload.get(b"prompt") or "", (bytes, bytearray))
                        else str(payload.get("prompt") or payload.get(b"prompt") or "")
                    )
                )
                
    return batch


def _collect_batch_result(batch: list[_JobEntry], call_handle: Any) -> None:
    """
    Wait for a spawned Modal batch call and persist final results per job.

    Args:
        batch: Collected jobs that were dispatched in a single run_batch call.
        call_handle: Modal spawn handle for run_batch.
    """
    try:
        boot_id, texts = call_handle.get()

        if len(texts) != len(batch):
            raise RuntimeError(
                f"batch result length mismatch: expected={len(batch)} got={len(texts)}"
            )

        for entry, text in zip(batch, texts, strict=True):
            text_str: str
            confidence_str: str | None = None
            confidence_num: float | None = None
            if isinstance(text, dict):
                confidence_val = text.get("confidence")
                confidence_num = _to_float(confidence_val)
                confidence_str = str(confidence_val) if confidence_val is not None else None
                raw_text = text.get("text")
                text_str = str(raw_text) if raw_text is not None else json.dumps(text)
            else:
                text_str = str(text)

            route_case_after_inference(entry, text_str, confidence_num)
            _mark_status(
                entry.job_id,
                status="done",
                boot_id=boot_id,
                text=text_str,
                confidence=confidence_str,
                finished_at=str(int(time.time())),
            )
            _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, entry.msg_id)
            _redis.xdel(_JOB_STREAM, entry.msg_id)

            # Mark as processed for idempotency
            reclaim_logic.mark_processed(_JOB_STREAM, entry.msg_id)

        logger.info("batch inference done: size=%s boot_id=%s", len(batch), boot_id)

    except Exception as e:
        logger.exception("batch inference failed: size=%s", len(batch))
        for entry in batch:
            _mark_status(
                entry.job_id,
                status="failed",
                error=str(e),
                finished_at=str(int(time.time())),
            )
            _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, entry.msg_id)
            _redis.xdel(_JOB_STREAM, entry.msg_id)

def _collect_result(job_id: str, msg_id: str, call_handle: Any) -> None:
    """
    Wait for a spawned Modal call and persist the final result.

    Args:
        job_id: The job ID
        msg_id: The Redis message ID
        call_handle: Modal spawn handle - A handle to a spawned Modal function that returns the inference result
    """
    try:
        boot_id, text = call_handle.get()
        text_str: str
        confidence: str | None = None
        if isinstance(text, dict):
            confidence_val = text.get("confidence")
            confidence = str(confidence_val) if confidence_val is not None else None
            text_str = json.dumps(text)
        else:
            text_str = str(text)
        logger.info("inference done: job_id=%s boot_id=%s text_len=%s", job_id, boot_id, len(text_str) if text_str is not None else 0)
        _mark_status(
            job_id,
            status="done",
            boot_id=boot_id,
            text=text_str,
            confidence=confidence,
            finished_at=str(int(time.time())),
        )
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        _redis.xdel(_JOB_STREAM, msg_id)

        # Mark as processed for idempotency
        reclaim_logic.mark_processed(_JOB_STREAM, msg_id)

        logger.info("message acked: msg_id=%s", msg_id)
    except Exception as e:
        logger.exception("inference failed: job_id=%s", job_id)
        _mark_status(job_id, status="failed", error=str(e), finished_at=str(int(time.time())))
        _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
        _redis.xdel(_JOB_STREAM, msg_id)
        logger.info("message acked after failure: msg_id=%s", msg_id)


def _start_reclaim_thread(
    stop_event: threading.Event,
) -> threading.Thread:
    """
    Start a background thread that periodically reclaims stale pending messages.

    Args:
        stop_event: Cooperative shutdown flag.

    Returns:
        The started reclaim thread.
    """
    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:
                def _reclaim_handler(msg_id: str, fields: dict[str, Any]) -> None:
                    # Mark as processed before handling to ensure idempotency
                    if reclaim_logic.mark_processed(_JOB_STREAM, msg_id):
                        try:
                            # Re-process the message by adding it back to the stream
                            # The message will be re-collected by the normal batch collection logic
                            _redis.xadd(_JOB_STREAM, fields)
                            # Ack and delete the reclaimed message
                            _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, msg_id)
                            _redis.xdel(_JOB_STREAM, msg_id)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                _JOB_STREAM, msg_id,
                            )
                            # Mark as not processed so it can be retried
                            _redis.delete(reclaim_logic.processed_key(_JOB_STREAM, msg_id))

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=_JOB_STREAM,
                    group=_CONSUMER_GROUP,
                    consumer=_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=_MEDGEMMA_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        _JOB_STREAM, reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", _JOB_STREAM)

            # Wait for the interval or stop event
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"medgemma-reclaim-{_JOB_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


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

    A future in this context is a placeholder for a result that will be available later.
    It represents an asynchronous operation that is currently in progress.

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

        # Initialize MedGemma service
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

        stop_event = threading.Event()

        # Start reclaim thread
        reclaim_thread = _start_reclaim_thread(stop_event)

        # Create thread pool (a pool of threads that can execute tasks concurrently) for processing jobs
        with ThreadPoolExecutor(max_workers=_MAX_INFLIGHT) as pool:

            # Set to track inflight collector tasks - Helps us track the available workers with respect to the maximum inflight limit
            inflight: set[Future[None]] = set()
            try:
                while not stop_event.is_set():
                    # Drain completed futures - This means, check if any futures have completed and remove them from the set
                    _drain_completed(inflight, timeout=0.0)

                    available_capacity: int = _MAX_INFLIGHT - len(inflight)
                    if available_capacity <= 0:

                        # If no capacity, wait for some futures to complete
                        _drain_completed(inflight, timeout=1.0)
                        continue

                    # Collect one micro-batch and dispatch as a single remote call.
                    batch: list[_JobEntry] = _collect_batch()
                    if not batch:
                        logger.debug("no messages available")
                        continue

                    requests_batch: list[tuple[bytes, str]] = [
                        (entry.image_bytes, entry.prompt) for entry in batch
                    ]

                    try:
                        call_handle = _dispatch_with_retry(service.run_batch.spawn, requests=requests_batch)
                        inflight.add(pool.submit(_collect_batch_result, batch, call_handle))
                        logger.info("batch dispatched: size=%s", len(batch))
                    except Exception as e:
                        logger.exception("batch dispatch failed: size=%s", len(batch))
                        for entry in batch:
                            _mark_status(
                                entry.job_id,
                                status="failed",
                                error=str(e),
                                finished_at=str(int(time.time())),
                            )
                            _redis.xack(_JOB_STREAM, _CONSUMER_GROUP, entry.msg_id)
                            _redis.xdel(_JOB_STREAM, entry.msg_id)
            except KeyboardInterrupt:
                logger.info("medgemma worker received KeyboardInterrupt; shutting down")
            finally:
                stop_event.set()
                reclaim_thread.join(timeout=5.0)
                logger.info("medgemma worker stopped")


if __name__ == "__main__":
    run_worker_forever()



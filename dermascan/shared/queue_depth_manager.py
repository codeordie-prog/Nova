"""
Author: Dermascan Team
Date: 2026-06-06
License: MIT
Description: Queue depth manager for Dermascan
This module implements a queue depth manager for the Dermascan service.
It uses Redis to track the number of jobs in the queue and limits the number of jobs in the queue.
The main logic here is to set up a queue depth manager that with respect to forward pass timeframe per Batch/request,
the input rate < drain rate of the REDIS STREAM - this avoids, backpressure and ensures the inference layer
is not overwhelmed.

"""

import os
import anyio
import redis
import logging

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

_JOB_STREAM: str = os.environ.get("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
_CONSUMER_GROUP: str = os.environ.get("ORCHESTRATOR_CONSUMER_GROUP", "orchestrator-workers")
_MAX_QUEUE_LEN_SOFT: int = max(1, int(os.environ.get("MAX_QUEUE_LEN_SOFT", 200)))
_MAX_QUEUE_LEN_HARD: int = max(_MAX_QUEUE_LEN_SOFT + 1, int(os.environ.get("MAX_QUEUE_LEN_HARD", 350)))
_API_WAIT_BUDGET_MS: int = max(0, int(os.environ.get("API_WAIT_BUDGET_MS", 200)))
_API_WAIT_STEP_MS: int = max(1, int(os.environ.get("API_WAIT_STEP_MS", 25)))

# Initialize logger
logger: logging.Logger = get_logger(__name__)

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """
    Return a lazily initialized Redis client.

    Returns:
        redis.Redis: Connected Redis client.

    Raises:
        RuntimeError: If REDIS_URL is not configured.
    """
    global _redis
    if _redis is None:
        _redis = get_redis_client(decode_responses=True)
    return _redis


def _ensure_consumer_group() -> None:
    """
    Ensure the consumer group exists for the job stream.
    """
    try:
        # Create the consumer group if it doesn't exist

        run_with_redis_retries(
            lambda: _get_redis().xgroup_create(_JOB_STREAM, _CONSUMER_GROUP, id="0", mkstream=True),
            operation_name="queue_depth_xgroup_create",
        )
        logger.info("consumer group created: stream=%s group=%s", _JOB_STREAM, _CONSUMER_GROUP)
    

    except Exception as e:
        if "BUSYGROUP" in str(e):
            logger.info("consumer group exists: stream=%s group=%s", _JOB_STREAM, _CONSUMER_GROUP)
            return
        raise

def _get_queue_depth() -> int:
    """
    
    Get the current queue depth using pending (delivered, not yet acknowledged) messages.
    This is more representative of actual backlog than raw XLEN, which includes tombstones.
    
    Returns:
        int: The current pending depth.
    """
    try:
        _ensure_consumer_group()
        summary = run_with_redis_retries(
            lambda: _get_redis().xpending(_JOB_STREAM, _CONSUMER_GROUP),
            operation_name="queue_depth_xpending",
        )
        pending_count: int = int(summary.get("pending", 0)) if isinstance(summary, dict) else 0
        return pending_count
    except redis.RedisError:
        raise
    
def _check_queue_pressure() -> tuple[str, int]:
    """
    Check the current queue pressure based on queue depth.
    
    Returns:
        tuple[str, int]: A tuple containing the pressure level ("green", "yellow", "red") and the current queue depth.
    """
    queue_length = _get_queue_depth()
    if queue_length < _MAX_QUEUE_LEN_SOFT:
        return "green", queue_length
    elif _MAX_QUEUE_LEN_SOFT <= queue_length < _MAX_QUEUE_LEN_HARD:
        return "yellow", queue_length
    else:
        return "red", queue_length

async def _wait_for_queue_capacity() -> tuple[str, int]:
    """
    Wait for queue capacity to become available.
    
    Returns:
        tuple[str, int]: A tuple containing the pressure level ("green", "yellow", "red") and the current queue depth.
    """
    elapsed_time = 0
    while elapsed_time < _API_WAIT_BUDGET_MS:
        state, depth = _check_queue_pressure()
        if state == "green":
            return state, depth
        
        # Hard overload, do not wait
        if state == "red":
            return state, depth
        # Yellow, wait briefly then retry
        await anyio.sleep(_API_WAIT_STEP_MS / 1000.0)
        elapsed_time += _API_WAIT_STEP_MS
    # Final check after budget is exhausted
    return _check_queue_pressure()


__all__ = ["_wait_for_queue_capacity"]

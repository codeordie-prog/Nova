"""
Author: Dermascan Team
Date: 2025-10-13
Description: Queue depth manager for MedGemma service

This module has helper functions that check the queue depth in REDIS

"""

import os
import anyio
import redis

from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env


_MAX_QUEUE_LEN_SOFT = max(1, int(os.environ.get("MAX_QUEUE_LEN_SOFT", 200)))
_MAX_QUEUE_LEN_HARD = max(_MAX_QUEUE_LEN_SOFT + 1, int(os.environ.get("MAX_QUEUE_LEN_HARD", 350)))
_API_WAIT_BUDGET_MS = max(0, int(os.environ.get("API_WAIT_BUDGET_MS", 150)))
_API_WAIT_STEP_MS = max(1, int(os.environ.get("API_WAIT_STEP_MS", 25)))

_JOB_STREAM = os.environ.get("MEDGEMMA_JOB_STREAM", "medgemma:jobs")

# get redis client
_redis: redis.Redis = redis_client_from_env().get_redis()

def _get_queue_depth() -> int:
    """
    Get the current queue depth.
    
    Returns:
        int: The current queue depth.
    """
    try:
        return _redis.xlen(_JOB_STREAM)
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

        # Safe continue
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

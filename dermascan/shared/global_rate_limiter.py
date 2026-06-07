"""
Author: Dermascan Team
Date: 2026-06-06
License: MIT
Description: Global rate limiter for Dermascan
This module implements a global rate limiter for the Dermascan service.
It uses Redis to track the number of requests per user and limits the number of requests per minute.
The main logic here is to set up an RPM that with respect to forward pass timeframe per Batch/request,
the input rate < drain rate of the REDIS STREAM - this avoids, backpressure and ensures the inference layer
is not overwhelmed.

"""

import os
import time

import anyio
from fastapi import HTTPException
from redis import Redis

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

_DEFAULT_CONFIGS: dict[str, int | str] = {
    "request_per_minute": 90,
    "window_seconds": 60,
    "key_prefix": "dermascan:rate_limit:global",
}

logger = get_logger(__name__)

_redis: Redis | None = None

_RPM: int = max(1, int(os.environ.get("WORKERS_GLOBAL_RPM", str(_DEFAULT_CONFIGS["request_per_minute"]))))
_WINDOW_S: int = max(1, int(os.environ.get("WORKERS_GLOBAL_RL_WINDOW_S", str(_DEFAULT_CONFIGS["window_seconds"]))))
_KEY_PREFIX: str = str(
    os.environ.get("WORKERS_GLOBAL_RL_KEY_PREFIX", str(_DEFAULT_CONFIGS["key_prefix"]))
).strip()


def _get_redis() -> Redis:
    """
    Return a lazily initialized Redis client.

    Returns:
        Redis: Connected Redis client.

    Raises:
        RuntimeError: If REDIS_URL is not configured.
    """
    global _redis
    if _redis is None:
        _redis = get_redis_client(decode_responses=True)
    return _redis


def _consume_global_window(cost: int) -> tuple[bool, int, int]:
    """
    Consume global rate-limit budget in Redis using a fixed window counter.

    Args:
        cost: Token cost for this request.

    Returns:
        tuple[bool, int, int]: (allowed, remaining, retry_after_seconds).
    """
    # Set the Key for current bucket
    now_window: int = int(time.time() // _WINDOW_S)
    key: str = f"{_KEY_PREFIX}:{now_window}"

    # Initialize a pipeline to execute multiple Redis commands 
    pipe = _get_redis().pipeline(transaction=False)

    # Atomically increment the counter by cost
    pipe.incrby(key, cost)
    # Set expiration for the key, nx=True ensures the key is not set if it already exists
    pipe.expire(key, _WINDOW_S + 1, nx=True)
    # Get the count and ttl for the key
    pipe.ttl(key)
    count_raw, _, ttl_raw = run_with_redis_retries(
        pipe.execute,
        operation_name="global_rate_limiter_pipeline_execute",
    )
    # Convert the raw values to integers
    count: int = int(count_raw)
    ttl_s: int = int(ttl_raw)
    if ttl_s <= 0:
        ttl_s = _WINDOW_S

    remaining: int = max(0, _RPM - count)
    allowed: bool = count <= _RPM
    retry_after_s: int = max(1, ttl_s)
    return allowed, remaining, retry_after_s


async def _enforce_global_rate_limit(*, cost: int = 1) -> None:
    """
    Enforce global distributed RPM limit.

    Args:
        cost: Token cost for the incoming request.

    Raises:
        HTTPException: If the request exceeds global rate limit.
    """
    if _RPM <= 0:
        return
    safe_cost: int = max(1, cost)
    allowed, _, retry_after_s = await anyio.to_thread.run_sync(_consume_global_window, safe_cost)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Global rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after_s)},
        )


__all__ = ["_enforce_global_rate_limit"]


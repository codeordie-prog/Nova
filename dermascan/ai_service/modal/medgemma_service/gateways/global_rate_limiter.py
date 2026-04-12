"""
Author: Dermascan Team
Date: 2026-04-12
Description: Global rate limiter for MedGemma service

This module implements a global rate limiter for the MedGemma service.
It uses Redis to track the number of requests per user and limits the number of requests per minute.

The main logic here is to set up an RPM that with respect to forward pass timeframe per Batch/request,
the input rate < drain rate of the REDIS STREAM - this avoids, backpressure and ensures the inference layer
is not overwhelmed.

By default RPM = 70, and a window of 60 seconds is used.
This can be adjusted in the gateways/config.json file with respect to Telemetry and Performance metrics.
"""

import json
import os
from pathlib import Path
import time

import anyio
import redis
from fastapi import HTTPException

from dermascan.ai_service.modal.medgemma_service.redis_client import redis_client_from_env
from dermascan.shared.logger import get_logger


_DEFAULT_CONFIGS: dict[str, int | str] = {
    "request_per_minute": 70,
    "window_seconds": 60,
    "key_prefix": "medgemma:rate_limit:global",
}

# Get logger
logger = get_logger(__name__)


def _load_config() -> dict[str, int | str]:
    """
    Load global rate limiter configuration from gateways/config.json.

    Returns:
        dict[str, int | str]: Configuration dictionary.
    """
    # Get path to configuration file for the global rate limiter
    cfg_path: Path = Path(__file__).with_name("config.json")
    if not cfg_path.exists():
        logger.warning("global rate limiter config not found: %s", cfg_path)
        return _DEFAULT_CONFIGS

    try:
        # Read and parse the configuration file
        raw: str = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            global_cfg = data.get("global_rate_limiter", data)
            if isinstance(global_cfg, dict):
                return {
                    "request_per_minute": int(
                        global_cfg.get("request_per_minute", _DEFAULT_CONFIGS["request_per_minute"])
                    ),
                    "window_seconds": int(
                        global_cfg.get("window_seconds", _DEFAULT_CONFIGS["window_seconds"])
                    ),
                    "key_prefix": str(global_cfg.get("key_prefix", _DEFAULT_CONFIGS["key_prefix"])),
                }
    except Exception:
        logger.warning("failed loading global rate limiter config; using defaults")

    return _DEFAULT_CONFIGS

# Load config
_config: dict[str, int | str] = _load_config()

_RPM: int = max(1, int(os.environ.get("MEDGEMMA_GLOBAL_RPM", str(_config["request_per_minute"]))))
_WINDOW_S: int = max(1, int(os.environ.get("MEDGEMMA_GLOBAL_RL_WINDOW_S", str(_config["window_seconds"]))))
_KEY_PREFIX: str = str(os.environ.get("MEDGEMMA_GLOBAL_RL_KEY_PREFIX", str(_config["key_prefix"]))).strip()

# Initialize Redis client
_redis: redis.Redis = redis_client_from_env().get_redis()


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
    pipe = _redis.pipeline(transaction=False)

    # Increment counter by cost
    pipe.incrby(key, cost)
    # Set expiration for the key
    pipe.expire(key, _WINDOW_S + 1, nx=True)
    # Get the time to live for the key
    pipe.ttl(key)
    count_raw, _, ttl_raw = pipe.execute()

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



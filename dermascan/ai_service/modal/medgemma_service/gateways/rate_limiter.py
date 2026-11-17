"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-3-12
Licence: PolyForm Noncommercial License 1.0.0
Description: Rate limiter for MedGemma service, with concurrency control and timeout handling.

Semaphore (Concurrency): Limits the number of concurrent requests to prevent resource exhaustion.

NVIDIA A10G GPU :
- Available vRAM: 24GB
- model's memory usage: params(4GB) * dtype(2bytes|bfloat16) = 8GB
- overhead - CUDA activation + other ops = ~4GB
- kv cache memory scale = sequence length(1024) * layers(32) * hidden_dimensions(128) * kv_heads(8 GQA) * 2 (for k and v) * 2 (for bfloat16) = ~0.125GB
- kv cache scales up : 0.125GB / request
- total memory usage : Cuda activation + model = ~ 12GB
- available vRAM = ~ 24GB - 12GB = ~ 12GB
- max concurrent requests (floor) = ~ available vRAM / kv cache overhead = ~ 12GB / 0.125GB = ~ 96 requests
- With this we can set the rate limiter configuration to 60 max concurrent requests per GPU


"""


import json
from pathlib import Path
import time

import anyio
import logging
from typing import List, Dict, Optional, Any, Callable

from fastapi import Request, HTTPException

from dermascan.shared.logger import get_logger

# Set default configuration
_DEFAULT_CONFIGS: Dict[str, Any] = {

    "rate_limiter": {
        "max_requests_per_minute": 60,
        "max_requests_per_hour": 1000
    },
    "rate_limit_window": 60,
    "timeout": 90,
    "max_concurrency": 60
}

# Initialize logger
logger: logging.Logger = get_logger(__name__)

# Get rate limiter configuration
def _load_config() -> dict:
    """
    Load rate limiter configuration from config.json
    
    Returns:
        dict: Configuration dictionary
    """
    # Get config file path
    cfg_path: Path = Path(__file__).with_name("config.json")
    logger.debug(f"Loading rate limiter config from: {cfg_path}")

    if not cfg_path.exists():
        logger.warning(f"Rate limiter config file not found at: {cfg_path}, falling back to default configurations")
        return _DEFAULT_CONFIGS
    
    try:
        # Read config file
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        logger.warning("Failed to load rate limiter config, falling back to default configurations")
    return _DEFAULT_CONFIGS


# Load configuration
config: dict = _load_config()


# Rate limit configuration
_RATE_LIMIT_RPM: int = int(config.get("rate_limiter", {}).get("max_requests_per_minute", 60))
_RATE_LIMIT_WINDOW_S: int = int(config.get("rate_limit_window", 60))

# Inference configuration
_INFER_TIMEOUT_S: float = float(config.get("timeout", 90))
_MAX_CONCURRENCY: int = int(config.get("max_concurrency", 60))


# Semaphore and state for rate limiting
_inference_semaphore: anyio.Semaphore = anyio.Semaphore(_MAX_CONCURRENCY)
_rate_limit_lock: anyio.Lock = anyio.Lock()
_rate_limit_state: dict[str, tuple[float, int]] = {}


# Get client's IP address
def _get_client_ip(request: Request) -> str:
    """
    Get the client's IP address from the request
    Useful when monitoring RPM rate for a particular client.
    Helps eradicate Spamming
    
    Args:
        request: FastAPI request object
        
    Returns:
        str: Client's IP address
    """
    # Get the IP address from the X-Forwarded-For(xff) header
    xff: str | None = request.headers.get("x-forwarded-for")

    if xff:
        logger.debug(f"X-Forwarded-For header: {xff}")

        # Extract the first IP address from the list
        return xff.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    # Fallback to the direct client IP
    return request.client.host


# Enforce rate limit
async def _enforce_rate_limit(*, request: Request) -> None:
    """
    Enforce rate limit for the given request
    
    Args:
        request: FastAPI request object
        
    Raises:
        HTTPException: If rate limit is exceeded
    """
    # If rate limit is disabled, return
    if _RATE_LIMIT_RPM <= 0:
        return

    # Get current time and client IP
    now: float = time.time()
    ip: str = _get_client_ip(request)
    logger.debug(f"Enforcing rate limit for IP: {ip}")

    # Check rate limit
    async with _rate_limit_lock:

        # Get the window start and count for the IP
        window_start, count = _rate_limit_state.get(ip, (now, 0))

        # If the window has expired, reset the count
        if now - window_start >= _RATE_LIMIT_WINDOW_S:
            window_start, count = now, 0

        # Increment the count
        count += 1
        _rate_limit_state[ip] = (window_start, count)
        if count > _RATE_LIMIT_RPM:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

# Run inference with guards
async def _run_inference_with_guards(fn: Callable[..., Any]) -> Any:
    """
    Run inference with guards
    
    Args:
        fn: Function to run
        
    Returns:
        Any: Result of the function
    """
    # Acquire the semaphore
    async with _inference_semaphore:
        # Set a timeout for the inference
        with anyio.fail_after(_INFER_TIMEOUT_S):
            return await anyio.to_thread.run_sync(fn)
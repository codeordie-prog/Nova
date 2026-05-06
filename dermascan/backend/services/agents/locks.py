"""
Author: Dermascan Team
Date: 2026-4-25

Description: Per-client Redis distributed lock.

A shared primitive for serializing operations per ``client_id`` across all
worker processes in the system.

Primary consumers:
    - **Orchestrator job flow**: prevents concurrent agent invokes on the same
      conversation ``thread_id`` from corrupting the shared checkpoint.
    - **Completing agents** (e.g., diagnose, physician-finalize): wrap the
      terminal event-stream + signal-stream publish so a concurrent user
      status check cannot receive stale "in progress" data after a newer
      proactive completion message has been sent.

Semantics:
    - Atomic acquisition via ``SET NX PX``.
    - A TTL is always set as a safety net for crashed holders.
    - Release is a Lua compare-and-delete so a worker cannot delete a lock
      whose TTL expired and was re-acquired by someone else.
"""

import os
import time
import uuid
import logging
from contextlib import contextmanager
from typing import Iterator, Optional

import redis

logger = logging.getLogger(__name__)

# Redis connection.
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Lock defaults. Callers may override per-call.
CLIENT_LOCK_KEY_PREFIX: str = os.getenv("CLIENT_LOCK_KEY_PREFIX", "orchestrator:lock:client")
CLIENT_LOCK_TTL_MS: int = int(os.getenv("CLIENT_LOCK_TTL_MS", "120000"))   # crashed-holder safety net
CLIENT_LOCK_WAIT_MS: int = int(os.getenv("CLIENT_LOCK_WAIT_MS", "50000"))  # max time to wait for the lock
CLIENT_LOCK_POLL_MS: int = int(os.getenv("CLIENT_LOCK_POLL_MS", "200"))    # poll interval while waiting

# Lua script for safe lock release (compare-and-delete).
# Deletes the key only if its current value still matches the caller's token,
# preventing a stale holder from releasing a lock that has since been
# re-acquired by another worker after TTL expiry.
_LOCK_RELEASE_LUA: str = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LockAcquisitionError(RuntimeError):
    """Raised when ``client_lock`` cannot acquire the lock within the wait budget."""


# Module-level client is created lazily so importing this module is side-effect
# free (useful for tests and for workers that configure Redis differently).
_redis_client: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
    """Return (and memoize) the module-level Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def client_lock_key(client_id: str) -> str:
    """
    Build the Redis key used to serialize operations for a single client.

    Args:
        client_id: The client identifier the lock is keyed on.

    Returns:
        The fully-qualified Redis key.
    """
    return f"{CLIENT_LOCK_KEY_PREFIX}:{client_id}"


def acquire_client_lock(
    client_id: str,
    *,
    wait_ms: Optional[int] = None,
    ttl_ms: Optional[int] = None,
    poll_ms: Optional[int] = None,
    redis_client: Optional[redis.Redis] = None,
) -> Optional[str]:
    """
    Try to acquire the per-client lock with bounded wait.

    Uses ``SET key token NX PX ttl_ms`` for atomic acquisition. Polls every
    ``poll_ms`` until either acquired or ``wait_ms`` elapses.

    Args:
        client_id: The client identifier the lock is keyed on.
        wait_ms: Override for ``CLIENT_LOCK_WAIT_MS``.
        ttl_ms: Override for ``CLIENT_LOCK_TTL_MS``.
        poll_ms: Override for ``CLIENT_LOCK_POLL_MS``.
        redis_client: Optional Redis client; falls back to the module default.

    Returns:
        The unique token to pass to ``release_client_lock`` on success, or
        ``None`` if the wait timed out.
    """
    r = redis_client or _get_redis()
    wait = CLIENT_LOCK_WAIT_MS if wait_ms is None else wait_ms
    ttl = CLIENT_LOCK_TTL_MS if ttl_ms is None else ttl_ms
    poll = CLIENT_LOCK_POLL_MS if poll_ms is None else poll_ms

    key = client_lock_key(client_id)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + (wait / 1000.0)
    poll_seconds = max(0.01, poll / 1000.0)

    while True:
        # SET NX PX is atomic: succeeds only if no other holder exists.
        if r.set(key, token, nx=True, px=ttl):
            return token
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_seconds)


def release_client_lock(
    client_id: str,
    token: str,
    *,
    redis_client: Optional[redis.Redis] = None,
) -> None:
    """
    Release the per-client lock if and only if we still own it.

    Best-effort: failures are logged but never raised, since releasing the
    lock should not block error reporting on the main flow.

    Args:
        client_id: The client identifier the lock is keyed on.
        token: The unique token obtained from ``acquire_client_lock``.
        redis_client: Optional Redis client; falls back to the module default.
    """
    r = redis_client or _get_redis()
    key = client_lock_key(client_id)
    try:
        r.eval(_LOCK_RELEASE_LUA, 1, key, token)
    except Exception:
        logger.exception("failed to release client lock: client_id=%s", client_id)


@contextmanager
def client_lock(
    client_id: str,
    *,
    wait_ms: Optional[int] = None,
    ttl_ms: Optional[int] = None,
    poll_ms: Optional[int] = None,
    redis_client: Optional[redis.Redis] = None,
) -> Iterator[str]:
    """
    Context manager wrapping ``acquire_client_lock`` / ``release_client_lock``.

    Raises:
        LockAcquisitionError: If the lock cannot be acquired within ``wait_ms``.

    Example:
        with client_lock(client_id):
            # critical section: write event stream + signal stream atomically
            ...
    """
    token = acquire_client_lock(
        client_id,
        wait_ms=wait_ms,
        ttl_ms=ttl_ms,
        poll_ms=poll_ms,
        redis_client=redis_client,
    )
    if token is None:
        raise LockAcquisitionError(
            f"could not acquire client lock for client_id={client_id} within "
            f"{wait_ms if wait_ms is not None else CLIENT_LOCK_WAIT_MS}ms"
        )
    try:
        yield token
    finally:
        release_client_lock(client_id, token, redis_client=redis_client)

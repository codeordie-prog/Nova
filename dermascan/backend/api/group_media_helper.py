"""
Redis-backed media-group buffering helpers.

This module batches channel media items that share one group identifier,
allowing downstream workers to receive grouped metadata with expected counts.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis import Redis


def media_batch_redis_key(channel: str, group_key: str) -> str:
    """
    Build Redis key for a media group list.

    Args:
        channel: Source channel name.
        group_key: Stable group id within channel.

    Returns:
        Redis key string.
    """
    return f"media_batch:{channel}:{group_key}"


def append_to_media_batch(
    redis_client: Redis,
    *,
    channel: str,
    group_key: str,
    slot: dict[str, Any],
    quiet_window_s: float,
) -> None:
    """
    Append one slot to a media-group buffer and refresh TTL.

    Args:
        redis_client: Redis client.
        channel: Source channel name.
        group_key: Group identifier.
        slot: Payload slot for one message/image.
        quiet_window_s: Quiet window in seconds for grouping.
    """
    key = media_batch_redis_key(channel, group_key)
    last_at_key = f"{key}:last_at"
    ttl_s = max(3, int(quiet_window_s) + 5)
    now_s = time.time()
    pipe = redis_client.pipeline()
    pipe.rpush(key, json.dumps(slot, default=str))
    pipe.expire(key, ttl_s)
    pipe.set(last_at_key, str(now_s))
    pipe.expire(last_at_key, ttl_s)
    pipe.execute()


async def wait_for_media_batch_quiet(
    redis_client: Redis,
    *,
    channel: str,
    group_key: str,
    quiet_window_s: float,
    max_wait_s: float,
) -> bool:
    """
    Wait until no new slots arrive for quiet_window_s.

    Args:
        redis_client: Redis client.
        channel: Source channel.
        group_key: Group identifier.
        quiet_window_s: Required quiet time before flush.
        max_wait_s: Maximum total wait duration.

    Returns:
        True if group became quiet, False on timeout.
    """
    key = media_batch_redis_key(channel, group_key)
    last_at_key = f"{key}:last_at"
    deadline = time.time() + max(quiet_window_s, max_wait_s)
    poll_s = 0.15

    while time.time() <= deadline:
        raw_last = redis_client.get(last_at_key)
        if raw_last is None:
            # Group already claimed/expired.
            return False
        try:
            last_ts = float(str(raw_last))
        except Exception:
            last_ts = time.time()
        if time.time() - last_ts >= quiet_window_s:
            return bool(redis_client.exists(key))
        await asyncio.sleep(poll_s)

    return False


def claim_media_batch(redis_client: Redis, *, channel: str, group_key: str) -> list[dict[str, Any]]:
    """
    Atomically claim one media group exactly once.

    Args:
        redis_client: Redis client.
        channel: Source channel.
        group_key: Group identifier.

    Returns:
        Parsed slot list. Empty when already claimed or unavailable.
    """
    key = media_batch_redis_key(channel, group_key)
    lock_key = f"{key}:claim_lock"
    last_at_key = f"{key}:last_at"

    now_s = time.time()
    lock_value = str(now_s)
    if not redis_client.set(lock_key, lock_value, nx=True, ex=120):
        try:
            existing_lock = redis_client.get(lock_key)
        except Exception:
            existing_lock = None
        try:
            lock_ts = float(str(existing_lock)) if existing_lock is not None else 0.0
        except Exception:
            lock_ts = 0.0
        try:
            raw_last = redis_client.get(last_at_key)
            last_ts = float(str(raw_last)) if raw_last is not None else 0.0
        except Exception:
            last_ts = 0.0
        if last_ts <= lock_ts:
            return []
        redis_client.set(lock_key, lock_value, ex=120)

    pipe = redis_client.pipeline()
    pipe.lrange(key, 0, -1)
    pipe.delete(key)
    pipe.delete(last_at_key)
    results = pipe.execute()
    raw_items = results[0] if isinstance(results, list) and results else []

    slots: list[dict[str, Any]] = []
    for raw in raw_items or []:
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, dict):
                slots.append(parsed)
        except Exception:
            continue
    return slots


def enrich_payload_with_group_metadata(
    *,
    payload_json: str,
    group_id: str,
    group_expected_count: int,
) -> str:
    """
    Inject group metadata into payload.metadata.extras.

    Args:
        payload_json: Existing payload JSON.
        group_id: Shared group id.
        group_expected_count: Expected number of group members.

    Returns:
        Updated payload JSON, original on parse failures.
    """
    try:
        payload_obj = json.loads(payload_json)
    except Exception:
        return payload_json
    if not isinstance(payload_obj, dict):
        return payload_json

    metadata_obj = payload_obj.get("metadata")
    if not isinstance(metadata_obj, dict):
        metadata_obj = {}
        payload_obj["metadata"] = metadata_obj
    extras_obj = metadata_obj.get("extras")
    if not isinstance(extras_obj, dict):
        extras_obj = {}
        metadata_obj["extras"] = extras_obj

    extras_obj["group_id"] = str(group_id)
    extras_obj["group_expected_count"] = str(max(1, int(group_expected_count)))
    return json.dumps(payload_obj, default=str)

"""
Author: Dermascan Team
Date: 2026-05-23
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: Redis stream pending message recovery utilities.

This module provides reusable functions for reclaiming stuck messages from
Redis Streams consumer groups using XAUTOCLAIM, along with idempotency
helpers to ensure reclaimed messages are not processed twice."""


import os
import json
import logging
from typing import Any, Optional, Callable

import redis

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

# Reclaim configuration environment variables
PENDING_MIN_IDLE_MS: int = max(0, int(os.getenv("WORKERS_STREAM_PENDING_MIN_IDLE_MS", "120000")))
RECLAIM_INTERVAL_SEC: int = max(1, int(os.getenv("WORKERS_RECLAIM_INTERVAL_SEC", "30")))
MAX_RECLAIM_ATTEMPTS: int = max(1, int(os.getenv("WORKERS_MAX_RECLAIM_ATTEMPTS", "3")))


def processed_key(stream: str, msg_id: str) -> str:
    """Build the Redis key for a processed-message marker."""
    return f"orchestrator:processed:{stream}:{msg_id}"


def mark_processed(stream: str, msg_id: str, ttl_sec: int = 3600) -> bool:
    """
    Mark a message as processed. Returns True if newly marked, False if already existed.
    Best-effort: failures are logged but never propagated.

    Args:
        stream: The stream key.
        msg_id: The message ID.
        ttl_sec: TTL for the processed marker (default 1 hour).

    Returns:
        True if the message was newly marked, False if it already existed.
    """
    key = processed_key(stream, msg_id)
    try:
        result = redis_client.set(key, "1", nx=True, ex=ttl_sec)
        return result is True  # SET NX returns True if key was set, False if already existed
    except Exception:
        logger.exception("failed to mark processed: stream=%s msg_id=%s", stream, msg_id)
        return False


def is_processed(stream: str, msg_id: str) -> bool:
    """
    Check if a message is already marked as processed. Best-effort.

    Args:
        stream: The stream key.
        msg_id: The message ID.

    Returns:
        True if the message is marked as processed, False otherwise.
    """
    key = processed_key(stream, msg_id)
    try:
        return redis_client.exists(key) == 1
    except Exception:
        logger.exception("failed to check processed: stream=%s msg_id=%s", stream, msg_id)
        return False


# Redis client will be initialized by the importing module
redis_client: Optional[redis.Redis] = None


def set_redis_client(client: redis.Redis) -> None:
    """
    Set the Redis client for this module.

    Args:
        client: The Redis client instance.
    """
    global redis_client
    redis_client = client


def _normalize_stream_fields(raw_fields: Any) -> dict[str, str]:
    """
    Normalize XAUTOCLAIM / stream entry fields from redis-py.

    redis-py may return either a flat [k1, v1, k2, v2, ...] list or a dict.
    """
    if isinstance(raw_fields, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw_fields.items()}

    if isinstance(raw_fields, (list, tuple)):
        fields_list = list(raw_fields)
        if not fields_list:
            return {}
        return dict(zip(fields_list[::2], fields_list[1::2]))

    return {}


def reclaim_stale_pending(
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[str, dict[str, Any]], None],
    dlq_stream: Optional[str] = None,
    min_idle_ms: int = PENDING_MIN_IDLE_MS,
    batch: int = 50,
) -> int:
    """
    Reclaim messages that have been pending (delivered but unacked) longer
    than `min_idle_ms` from any consumer in the group, and re-deliver them
    to THIS consumer for processing. Idempotent and safe to call often.

    Args:
        stream: The Redis stream key.
        group: The consumer group name.
        consumer: The consumer name to reclaim messages to.
        handler: Callable invoked per reclaimed message: ``handler(msg_id, fields)``.
        dlq_stream: Optional DLQ stream for messages that exceed max reclaim attempts.
        min_idle_ms: Minimum idle time in ms before a message is considered stale.
        batch: Maximum messages to reclaim per call.

    Returns:
        Number of messages reclaimed in this pass.
    """
    if redis_client is None:
        logger.warning("redis_client not set; skipping reclaim")
        return 0

    cursor = "0-0"
    reclaimed_count = 0

    while True:
        try:
            # XAUTOCLAIM <stream> <group> <consumer> <min-idle-ms> <start-id> COUNT <n>
            # Returns: (next_cursor, claimed_messages, deleted_ids)
            cursor, claimed, _ = redis_client.execute_command(
                "XAUTOCLAIM", stream, group, consumer,
                min_idle_ms, cursor, "COUNT", batch,
            )
        except Exception:
            logger.exception("XAUTOCLAIM failed: stream=%s group=%s", stream, group)
            break

        if not claimed:
            break

        # claimed entries: [msg_id, fields] where fields may be dict or flat list
        for item in claimed:
            if not item:
                continue
            msg_id = item[0]
            raw_fields = item[1] if len(item) > 1 else {}
            fields = _normalize_stream_fields(raw_fields)

            # Check idempotency first to avoid double processing
            if is_processed(stream, msg_id):
                logger.info(
                    "reclaimed message already processed; acking and deleting: stream=%s msg_id=%s",
                    stream, msg_id,
                )
                try:
                    redis_client.xack(stream, group, msg_id)
                    redis_client.xdel(stream, msg_id)
                except Exception:
                    logger.exception("failed to ack/del already-processed reclaimed message: stream=%s msg_id=%s", stream, msg_id)
                continue

            # Check reclaim attempts
            attempt_key = f"orchestrator:reclaim_attempts:{stream}:{msg_id}"
            try:
                attempts = redis_client.incr(attempt_key)
                redis_client.expire(attempt_key, 86400)  # 24 hours
            except Exception:
                logger.exception("failed to track reclaim attempts: stream=%s msg_id=%s", stream, msg_id)
                attempts = 1

            if attempts > MAX_RECLAIM_ATTEMPTS:
                logger.warning(
                    "message exceeded max reclaim attempts; sending to DLQ: stream=%s msg_id=%s attempts=%s",
                    stream, msg_id, attempts,
                )
                if dlq_stream:
                    try:
                        redis_client.xadd(
                            dlq_stream,
                            {
                                "original_stream": stream,
                                "original_msg_id": msg_id,
                                "original_group": group,
                                "reclaim_attempts": str(attempts),
                                "reason": "max_reclaim_attempts_exceeded",
                                **fields,
                            },
                        )
                        redis_client.xack(stream, group, msg_id)
                        redis_client.xdel(stream, msg_id)
                    except Exception:
                        logger.exception("failed to send to DLQ: stream=%s msg_id=%s", stream, msg_id)
                continue

            # Process the reclaimed message
            try:
                handler(msg_id, fields)
                reclaimed_count += 1
            except Exception:
                logger.exception("handler failed for reclaimed message: stream=%s msg_id=%s", stream, msg_id)
                # Leave it pending; next reclaim cycle will retry

    return reclaimed_count

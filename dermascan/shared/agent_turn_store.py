"""
Author: Dermascan Team
Date: 11-20-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Redis persistence for agent API turn ingress metadata and completed replies."""


from __future__ import annotations

import json
import os
from typing import Any, Final

import redis

_AGENT_INGRESS_PREFIX: Final[str] = "agent:ingress:"
_AGENT_TURN_PREFIX: Final[str] = "agent:turn:"
_DEFAULT_TTL_S: int = int(os.getenv("AGENT_TURN_TTL_S", "86400"))


def ingress_key(request_id: str) -> str:
    """
    Build the Redis key for turn ingress metadata.

    Args:
        request_id: Orchestrator request id.

    Returns:
        str: Redis key.
    """
    return f"{_AGENT_INGRESS_PREFIX}{request_id}"


def turn_reply_key(request_id: str) -> str:
    """
    Build the Redis key for a completed agent turn reply.

    Args:
        request_id: Orchestrator request id.

    Returns:
        str: Redis key.
    """
    return f"{_AGENT_TURN_PREFIX}{request_id}"


def save_ingress_meta(
    redis_client: redis.Redis,
    *,
    request_id: str,
    client_id: str,
    case_id: str,
    channel: str = "web",
    ttl_s: int | None = None,
) -> None:
    """
    Record ingress metadata so poll endpoints can resolve unknown request ids.

    Args:
        redis_client: Redis client (decode_responses=True).
        request_id: New orchestrator request id.
        client_id: Stable external client id.
        case_id: Conversation case id.
        channel: Ingress channel label.
        ttl_s: Optional TTL override.

    Raises:
        redis.RedisError: On Redis write failure.
    """
    payload: dict[str, str] = {
        "request_id": request_id,
        "client_id": client_id,
        "case_id": case_id,
        "channel": channel,
    }
    redis_client.setex(ingress_key(request_id), ttl_s or _DEFAULT_TTL_S, json.dumps(payload))


def load_ingress_meta(redis_client: redis.Redis, request_id: str) -> dict[str, Any] | None:
    """
    Load ingress metadata for a request id.

    Args:
        redis_client: Redis client (decode_responses=True).
        request_id: Orchestrator request id.

    Returns:
        dict[str, Any] | None: Parsed metadata or None if missing.
    """
    raw = redis_client.get(ingress_key(request_id))
    if not raw:
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def save_turn_reply(
    redis_client: redis.Redis,
    *,
    request_id: str,
    client_id: str,
    case_id: str,
    text: str,
    kind: str = "job_reply",
    ttl_s: int | None = None,
) -> None:
    """
    Persist the user-visible reply for ``channel=web`` polling clients.

    Args:
        redis_client: Redis client (decode_responses=True).
        request_id: Orchestrator request id.
        client_id: Client id from outbound fields.
        case_id: Case id from outbound fields.
        text: Assistant reply text.
        kind: Outbound kind label.
        ttl_s: Optional TTL override.

    Raises:
        redis.RedisError: On Redis write failure.
    """
    body = text.strip()
    if not body:
        return
    payload: dict[str, str] = {
        "request_id": request_id,
        "client_id": client_id,
        "case_id": case_id,
        "status": "done",
        "text": body,
        "kind": kind,
    }
    redis_client.setex(turn_reply_key(request_id), ttl_s or _DEFAULT_TTL_S, json.dumps(payload))


def load_turn_reply(redis_client: redis.Redis, request_id: str) -> dict[str, Any] | None:
    """
    Load a completed turn reply for polling.

    Args:
        redis_client: Redis client (decode_responses=True).
        request_id: Orchestrator request id.

    Returns:
        dict[str, Any] | None: Reply payload or None.
    """
    raw = redis_client.get(turn_reply_key(request_id))
    if not raw:
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_interim_product_outbound(fields: dict[str, Any]) -> bool:
    """
    Return True for product-review progress outbounds that must not complete web poll.

    Args:
        fields: Outbound stream fields (must include ``dedupe_key`` when set at enqueue).

    Returns:
        bool: True when the outbound is the deferred index-pending interim message.
    """
    dedupe_key = str(fields.get("dedupe_key") or "")
    return ":index_pending:" in dedupe_key


def should_persist_web_agent_turn(fields: dict[str, Any]) -> bool:
    """
    Decide whether an outbound should be stored for ``GET /v1/agents/turns/{id}``.

    Job replies and final signal reports are persisted; product index-pending
    progress messages are skipped so clients keep polling until the full report.

    Args:
        fields: Outbound stream fields.

    Returns:
        bool: True when the message should be written to the agent turn store.
    """
    kind = str(fields.get("kind") or "").strip().lower()
    if kind == "job_reply":
        return True
    if kind == "signal_update":
        return not is_interim_product_outbound(fields)
    return False


def outbound_field_is_poll_terminal(fields: dict[str, Any]) -> bool:
    """
    Return True when an outbound stream entry can complete agent turn polling.

    Args:
        fields: Outbound stream fields.

    Returns:
        bool: True for job replies and non-interim signal updates.
    """
    return should_persist_web_agent_turn(fields)
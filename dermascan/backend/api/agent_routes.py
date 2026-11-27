"""
Author: Dermascan Team
Date: 11-20-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

HTTP API for external agent clients (OpenClaw plugin, future MCP/Actions)."""


from __future__ import annotations

import base64
import binascii
import os
import uuid
from datetime import datetime
import redis
from fastapi import APIRouter, Depends, HTTPException, Request

from dermascan.backend.api.endpoints import (
    _enqueue_orchestrator_row,
    _enforce_ingress_capacity,
    _store_uploaded_client_image,
    _validate_image_bytes,
)
from dermascan.backend.models.agent_api import (
    AgentTurnCreateRequest,
    AgentTurnCreateResponse,
    AgentTurnStatusResponse,
)
from dermascan.backend.models.requests import (
    AttachmentItem,
    AttachmentSource,
    MessageContent,
    OrchestratorIngressRequest,
    RequestMetadata,
    RoutingHint,
)
from dermascan.backend.services.agents import reclaim_logic
from dermascan.shared.agent_api_auth import verify_agent_api_bearer
from dermascan.shared.agent_turn_store import (
    load_ingress_meta,
    load_turn_reply,
    outbound_field_is_poll_terminal,
    save_ingress_meta,
)
from dermascan.shared.load_env import bootstrap_env
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

bootstrap_env()

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["agents"])

_REDIS_URL: str = os.getenv("REDIS_URL", "")
_JOB_STREAM: str = os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs")
_OUTBOUND_STREAM: str = os.getenv("OUTBOUND_STREAM", "outbound:messages")
_JOB_SCAN_COUNT: int = int(os.getenv("AGENT_API_JOB_SCAN_COUNT", "500"))
_OUTBOUND_SCAN_COUNT: int = int(os.getenv("AGENT_API_OUTBOUND_SCAN_COUNT", "500"))

_redis = get_redis_client(decode_responses=True) if _REDIS_URL else None


def _require_redis() -> redis.Redis:
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return _redis


def _find_job_message_id(redis_client: redis.Redis, request_id: str) -> str | None:
    """
    Locate the orchestrator job stream id for a request id.

    Args:
        redis_client: Redis client.
        request_id: Orchestrator request id.

    Returns:
        str | None: Stream message id when found.
    """
    entries = redis_client.xrevrange(_JOB_STREAM, count=_JOB_SCAN_COUNT)
    for msg_id, fields in entries:
        if str(fields.get("request_id") or "") == request_id:
            return str(msg_id)
    return None


def _find_outbound_text(redis_client: redis.Redis, request_id: str) -> tuple[str | None, str | None]:
    """
    Scan the outbound stream for the latest non-empty reply text.

    Args:
        redis_client: Redis client.
        request_id: Orchestrator request id.

    Returns:
        Tuple of (text, kind) when found, else (None, None).
    """
    entries = redis_client.xrevrange(_OUTBOUND_STREAM, count=_OUTBOUND_SCAN_COUNT)
    for _, fields in entries:
        if str(fields.get("request_id") or "") != request_id:
            continue
        if not outbound_field_is_poll_terminal(fields):
            continue
        kind = str(fields.get("kind") or "").strip().lower() or "job_reply"
        text = str(fields.get("text") or "").strip()
        if text:
            return text, kind
    return None, None


def _build_attachments(
    body: AgentTurnCreateRequest,
    *,
    client_id: str,
    message_id: str,
    redis_client: redis.Redis,
) -> list[AttachmentItem]:
    """
    Decode optional base64 image into orchestrator attachment items.

    Args:
        body: Turn create request.
        client_id: Client id.
        message_id: Message id for storage paths.
        redis_client: Redis client.

    Returns:
        list[AttachmentItem]: Zero or one image attachment.

    Raises:
        HTTPException: When image bytes are invalid.
    """
    if not body.image_base64:
        return []
    try:
        image_bytes = base64.b64decode(body.image_base64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid image_base64") from exc
    _validate_image_bytes(image_bytes)
    source, _, _ = _store_uploaded_client_image(
        channel="web",
        client_id=client_id,
        message_id=message_id,
        image_bytes=image_bytes,
        mime_type=body.image_mime_type.strip() or "image/jpeg",
    )
    return [
        AttachmentItem(
            id=f"web-{uuid.uuid4().hex}",
            type="image",
            mime_type=body.image_mime_type.strip() or "image/jpeg",
            filename=None,
            size_bytes=len(image_bytes),
            source=source,
        )
    ]


def _normalize_agent_turn(
    body: AgentTurnCreateRequest,
    *,
    request_id: str,
    case_id: str,
    message_id: str,
    redis_client: redis.Redis,
) -> OrchestratorIngressRequest:
    """
    Map an agent API body to the orchestrator ingress envelope.

    Args:
        body: Turn create request.
        request_id: Generated request id.
        case_id: Resolved case id.
        message_id: Generated message id.
        redis_client: Redis client for image storage.

    Returns:
        OrchestratorIngressRequest: Normalized ingress payload.

    Raises:
        HTTPException: When the message has no usable content.
    """
    text = (body.text or "").strip() or None
    attachments = _build_attachments(body, client_id=body.client_id, message_id=message_id, redis_client=redis_client)
    content = MessageContent(text=text, attachments=attachments)
    if not content.text and not content.attachments:
        raise HTTPException(status_code=400, detail="text or image_base64 is required")

    extras: dict[str, str] = {}
    if body.source:
        extras["agent_source"] = body.source.strip()
    extras["agent_api"] = "v1"

    return OrchestratorIngressRequest(
        request_id=request_id,
        case_id=case_id,
        client_id=body.client_id.strip(),
        message_id=message_id,
        conversation_id=case_id,
        username=(body.username or "").strip() or None,
        channel="web",
        timestamp=datetime.utcnow(),
        content=content,
        routing=RoutingHint(ingress_stream=_JOB_STREAM, priority="normal"),
        metadata=RequestMetadata(extras=extras),
    )


@router.post("/turns", response_model=AgentTurnCreateResponse)
async def create_agent_turn(
    body: AgentTurnCreateRequest,
    request: Request,
    _: None = Depends(verify_agent_api_bearer),
) -> AgentTurnCreateResponse:
    """
    Enqueue one orchestrator turn for an external agent client.

    Args:
        body: Turn payload.
        request: HTTP request (auth headers).

    Returns:
        AgentTurnCreateResponse: Queued acknowledgement with ids.
    """
    _ = request
    redis_client = _require_redis()
    client_id = body.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")

    case_id = (body.case_id or "").strip() or uuid.uuid4().hex
    request_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex

    normalized = _normalize_agent_turn(
        body,
        request_id=request_id,
        case_id=case_id,
        message_id=message_id,
        redis_client=redis_client,
    )

    await _enforce_ingress_capacity(cost=1)

    try:
        save_ingress_meta(
            redis_client,
            request_id=request_id,
            client_id=client_id,
            case_id=case_id,
            channel="web",
        )

        # Enqueue the orchestrator row
        _enqueue_orchestrator_row(normalized=normalized)
    except redis.RedisError as exc:
        logger.exception("agent turn enqueue failed: request_id=%s", request_id)
        raise HTTPException(status_code=503, detail="Redis unavailable") from exc

    # Return the response - Poll using the request_id
    return AgentTurnCreateResponse(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        status="queued",
    )


@router.get("/turns/{request_id}", response_model=AgentTurnStatusResponse)
async def get_agent_turn(
    request_id: str,
    _: None = Depends(verify_agent_api_bearer),
) -> AgentTurnStatusResponse:
    """
    Poll turn status and assistant text for an external agent client.

    Args:
        request_id: Id returned from ``POST /v1/agents/turns``.

    Returns:
        AgentTurnStatusResponse: Status and optional reply text.
    """
    rid = request_id.strip()
    if not rid:
        raise HTTPException(status_code=400, detail="request_id is required")

    redis_client = _require_redis()
    meta = load_ingress_meta(redis_client, rid)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown request_id")

    client_id = str(meta.get("client_id") or "")
    case_id = str(meta.get("case_id") or "")

    cached = load_turn_reply(redis_client, rid)
    if cached is not None:
        return AgentTurnStatusResponse(
            request_id=rid,
            case_id=str(cached.get("case_id") or case_id),
            client_id=str(cached.get("client_id") or client_id),
            status="done",
            text=str(cached.get("text") or "") or None,
            kind=str(cached.get("kind") or "") or None,
        )

    outbound_text, outbound_kind = _find_outbound_text(redis_client, rid)
    if outbound_text:
        return AgentTurnStatusResponse(
            request_id=rid,
            case_id=case_id,
            client_id=client_id,
            status="done",
            text=outbound_text,
            kind=outbound_kind,
        )

    job_msg_id = _find_job_message_id(redis_client, rid)
    processed_key = (
        reclaim_logic.processed_key(_JOB_STREAM, job_msg_id) if job_msg_id else ""
    )
    if job_msg_id and processed_key and redis_client.exists(processed_key) == 1:
        return AgentTurnStatusResponse(
            request_id=rid,
            case_id=case_id,
            client_id=client_id,
            status="running",
        )

    if job_msg_id:
        return AgentTurnStatusResponse(
            request_id=rid,
            case_id=case_id,
            client_id=client_id,
            status="queued",
        )

    return AgentTurnStatusResponse(
        request_id=rid,
        case_id=case_id,
        client_id=client_id,
        status="unknown",
        error="job not found in orchestrator stream",
    )
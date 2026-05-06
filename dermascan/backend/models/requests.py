"""
Author: Dermascan Team
Date: 2026-04-25

Description: Request/response schemas for backend orchestrator ingress endpoints.


"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


AttachmentType = Literal["image", "document", "audio", "video"]
SourceKind = Literal["whatsapp_media_id", "url", "base64", "blob_ref"]
Priority = Literal["low", "normal", "high"]
Channel = Literal["whatsapp", "telegram", "web", "internal"]


class AttachmentSource(BaseModel):
    """Location descriptor for an attachment payload."""

    kind: SourceKind
    value: str = Field(min_length=1)


class AttachmentItem(BaseModel):
    """One multimodal attachment associated with the user message."""

    id: str = Field(min_length=1)
    type: AttachmentType
    mime_type: str = Field(min_length=1)
    filename: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    source: AttachmentSource


class MessageContent(BaseModel):
    """Normalized user message body supporting text and attachments."""

    text: Optional[str] = None
    attachments: list[AttachmentItem] = Field(default_factory=list)


class RoutingHint(BaseModel):
    """Routing hints used by orchestrator ingress before downstream fan-out."""

    ingress_stream: str = "orchestrator:jobs"
    priority: Priority = "normal"
    target_agent: Optional[Literal["orchestrator", "diagnose", "knowledge", "escalation", "inference"]] = None


class RequestMetadata(BaseModel):
    """Optional metadata for traceability and tenant/channel context."""

    tenant_id: Optional[str] = None
    locale: Optional[str] = None
    trace_id: Optional[str] = None
    extras: dict[str, Any] = Field(default_factory=dict)


class OrchestratorIngressRequest(BaseModel):
    """Canonical ingress envelope for orchestrator job intake."""

    request_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    conversation_id: Optional[str] = None
    username: Optional[str] = None
    channel: Channel = "whatsapp"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    content: MessageContent
    routing: RoutingHint = Field(default_factory=RoutingHint)
    metadata: RequestMetadata = Field(default_factory=RequestMetadata)


class OrchestratorBatchIngressRequest(BaseModel):
    """Batch variant for submitting multiple normalized ingress requests."""

    requests: list[OrchestratorIngressRequest] = Field(min_length=1)


class EnqueueAckResponse(BaseModel):
    """Minimal enqueue acknowledgement returned to callers."""

    request_id: str
    status: Literal["queued"] = "queued"


class BatchEnqueueAckResponse(BaseModel):
    """Batch enqueue acknowledgement payload."""

    request_ids: list[str]
    status: Literal["queued"] = "queued"


class RequestStatusResponse(BaseModel):
    """Orchestrator job status read model for polling clients."""

    request_id: str
    case_id: str
    client_id: str
    status: Literal["queued", "running", "done", "failed"]
    provider_used: Optional[str] = None
    error: Optional[str] = None
    updated_at: Optional[datetime] = None

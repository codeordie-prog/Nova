# Event schemas (DiagnoseRequested, DiagnoseCompleted, EscalationOpened, etc.)

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    """Common fields for all events."""

    case_id: str
    request_id: str
    ts: datetime = Field(default_factory=datetime.utcnow)
    user_id: Optional[str] = None
    agent: Optional[str] = None  # emitting agent name/id
    source: Optional[str] = None  # e.g., "frontend", "orchestrator"


class DiagnoseRequested(BaseEvent):
    """Triggered when a user requests a diagnosis."""

    image_ref: Optional[str] = None
    prompt: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DiagnoseCompleted(BaseEvent):
    """Triggered when a diagnosis is completed."""

    diagnosis: str
    confidence: float
    model_version: Optional[str] = None
    rationale: Optional[str] = None


class KnowledgeRequested(BaseEvent):
    """Triggered when knowledge for a product/condition/medication is requested."""

    topic: str
    image_ref: Optional[str] = None
    scope: Optional[str] = None


class KnowledgeRetrieved(BaseEvent):
    """Triggered when knowledge is retrieved."""

    content_ref: str  # pointer to cached content/KB
    summary: Optional[str] = None


class EscalationOpened(BaseEvent):
    """Triggered when an escalation is opened."""

    reason: Optional[str] = None
    confidence: Optional[float] = None
    threshold: Optional[float] = None


class EscalationClosed(BaseEvent):
    """Triggered when an escalation is closed."""

    resolved_by: Optional[str] = None  # agent/physician id
    note: Optional[str] = None


class PhysicianNoteOpened(BaseEvent):
    """Triggered when a physician note is opened."""

    physician_id: Optional[str] = None
    note: Optional[str] = None


class CaseClosed(BaseEvent):
    """Triggered when a case is closed."""

    resolution: Optional[str] = None
    closed_by: Optional[str] = None

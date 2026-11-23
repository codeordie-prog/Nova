"""Author: Dermascan Team
Date: 11-20-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Request and response models for the external agent HTTP API."""


from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentTurnStatus = Literal["queued", "running", "done", "failed", "unknown"]


class AgentTurnCreateRequest(BaseModel):
    """Body for ``POST /v1/agents/turns``."""

    client_id: str = Field(
        min_length=1,
        description="Stable external client id, e.g. openclaw:<session>",
    )
    case_id: str | None = Field(
        default=None,
        description="Conversation case id; defaults to a new id when omitted",
    )
    text: str | None = Field(default=None, description="User message text")
    image_base64: str | None = Field(
        default=None,
        description="Optional JPEG/PNG/GIF/WebP image as base64",
    )
    image_mime_type: str = Field(default="image/jpeg")
    source: str | None = Field(
        default=None,
        description="Optional caller label stored in metadata (e.g. openclaw)",
    )
    username: str | None = Field(default=None, description="Optional display name")


class AgentTurnCreateResponse(BaseModel):
    """Acknowledgement for a submitted agent turn."""

    request_id: str
    case_id: str
    client_id: str
    status: Literal["queued"] = "queued"


class AgentTurnStatusResponse(BaseModel):
    """Poll result for ``GET /v1/agents/turns/{request_id}``."""

    request_id: str
    case_id: str
    client_id: str
    status: AgentTurnStatus
    text: str | None = None
    kind: str | None = None
    error: str | None = None
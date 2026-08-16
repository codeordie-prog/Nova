"""
Author: Dermascan Team
Date: 2025-10-16
License: MIT

Schemas for web endpoints, with Pydantic models for request and response validation.
"""

from typing import List
from typing import Optional

from pydantic import BaseModel, Field, model_validator

# Pydantic model for base64 request.
class DiagnoseBase64Request(BaseModel):
    """JSON request schema for base64 image diagnosis."""

    image_base64: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    request_id: Optional[str] = None
    case_id: Optional[str] = None
    client_id: Optional[str] = None
    message_id: Optional[str] = None
    channel: Optional[str] = None
    group_id: Optional[str] = None
    group_expected_count: Optional[int] = None


class AuditImageRefRequest(BaseModel):
    """JSON request schema for image auditing via URL or durable blob path."""

    request_id: str = Field(min_length=1)
    image_url: Optional[str] = None
    image_blob_path: Optional[str] = None
    context: str = Field(min_length=1)
    case_id: Optional[str] = None
    client_id: Optional[str] = None
    message_id: Optional[str] = None
    channel: Optional[str] = None
    mime_type: Optional[str] = None
    group_id: Optional[str] = None
    group_expected_count: Optional[int] = None

    @model_validator(mode="after")
    def _require_image_reference(self) -> "AuditImageRefRequest":
        """
        Ensure at least one image reference is provided.

        Returns:
            Validated request instance.

        Raises:
            ValueError: When both image references are missing.
        """
        if not str(self.image_blob_path or "").strip() and not str(self.image_url or "").strip():
            raise ValueError("image_blob_path or image_url is required")
        return self

# Pydantic model for response.
class DiagnoseResponse(BaseModel):
    """
    Response schema for diagnose endpoint.
    """
    boot_id: str
    text: str


# Pydantic model for batch response.
class DiagnoseBatchResponse(BaseModel):
    """Response schema for diagnose_batch endpoint."""

    boot_id: str
    texts: List[str]

# Pydantic model for enqueue response.
class EnqueueResponse(BaseModel):
    """
    Response schema for enqueue endpoint.
    """
    job_id: str
    status: str


class BatchEnqueueResponse(BaseModel):
    """
    Response schema for batch enqueue endpoint.
    """

    job_ids: List[str]
    status: str


# Pydantic model for job status response.
class JobStatusResponse(BaseModel):
    """
    Response schema for job status endpoint.
    """
    job_id: str
    status: str
    boot_id: Optional[str] = None
    text: Optional[str] = None
    confidence: Optional[float] = None
    error: Optional[str] = None




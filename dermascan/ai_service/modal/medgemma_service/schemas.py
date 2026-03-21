"""
Author: Dermascan Team
Date: 2025-10-16
License: MIT

Schemas for web endpoints, with Pydantic models for request and response validation.
"""

from typing import List
from typing import Optional

from pydantic import BaseModel, Field

# Pydantic model for base64 request.
class DiagnoseBase64Request(BaseModel):
    """JSON request schema for base64 image diagnosis."""

    image_base64: str = Field(min_length=1)
    prompt: str = Field(min_length=1)

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
    error: Optional[str] = None




"""
Author: Dermascan Team
Date: 2025-10-16
License: MIT

Schemas for web endpoints, with Pydantic models for request and response validation.
"""

from typing import List

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




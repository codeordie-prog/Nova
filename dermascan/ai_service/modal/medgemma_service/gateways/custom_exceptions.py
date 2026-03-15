"""
Author: Dermascan Team
Date: 2026-3-12
License: MIT
Description: Custom exceptions for MedGemma service.
"""

from __future__ import annotations


class MedGemmaError(Exception):
    """Base exception for MedGemma web service errors."""


class ImageValidationError(MedGemmaError):
    """Raised when an uploaded image fails validation."""

    status_code: int = 400
    detail: str = "Invalid image"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        if detail is not None:
            self.detail = detail


class EmptyImageError(ImageValidationError):
    """Raised when the upload is empty."""

    status_code = 400
    detail = "Empty image upload"


class ImageTooLargeError(ImageValidationError):
    """Raised when the upload exceeds the allowed size."""

    status_code = 413

    def __init__(self, *, max_bytes: int) -> None:
        super().__init__(f"Image too large (max {max_bytes} bytes)")


class UnsupportedImageFormatError(ImageValidationError):
    """Raised when the payload is not a supported image type."""

    status_code = 400
    detail = "Unsupported or invalid image format"


# Export all exceptions
__all__ = [
    "MedGemmaError",
    "ImageValidationError",
    "EmptyImageError",
    "ImageTooLargeError",
    "UnsupportedImageFormatError",
]

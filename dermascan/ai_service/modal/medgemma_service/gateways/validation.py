"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: DermaScan Team
Date: 2026-03-12
Description: Image validation utilities for MedGemma service
"""


from fastapi import UploadFile

from dermascan.ai_service.modal.medgemma_service.gateways.custom_exceptions import (
    EmptyImageError,
    ImageTooLargeError,
    UnsupportedImageFormatError,
)

MAX_IMAGE_BYTES: int = 5 * 1024 * 1024
_MAX_IMAGE_BYTES: int = MAX_IMAGE_BYTES

# Validate image bytes
def _validate_image_bytes(image_bytes: bytes) -> None:
    """
    Validate image bytes for size and format.
    
    Args:
        image_bytes: Image bytes to validate
        
    Raises:
        Exception: If image is invalid
    """
    if not image_bytes:
        raise EmptyImageError()

    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise ImageTooLargeError(max_bytes=_MAX_IMAGE_BYTES)

    # Check image format
    head: bytes = image_bytes[:16]
    is_jpeg: bool = head.startswith(b"\xff\xd8\xff")
    is_png: bool = head.startswith(b"\x89PNG\r\n\x1a\n")
    is_gif: bool = head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    is_webp: bool = len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"

    # Check if image format is supported
    if not (is_jpeg or is_png or is_gif or is_webp):
        raise UnsupportedImageFormatError()


# Read upload file with size limit
async def _read_upload_limited(upload: UploadFile, *, max_bytes: int) -> bytes:
    """
    Read upload file with size limit.
    
    Args:
        upload: Upload file to read
        max_bytes: Maximum bytes to read
        
    Returns:
        bytes: Read bytes
        
    Raises:
        Exception: If upload is too large
    """
    # Initialize buffer and chunk size
    buf: bytearray = bytearray()
    chunk_size: int = min(1024 * 1024, max_bytes)
    
    # Read chunks until max bytes or end of file
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ImageTooLargeError(max_bytes=max_bytes)
    return bytes(buf)


__all__ = ["MAX_IMAGE_BYTES", "_validate_image_bytes", "_read_upload_limited"]

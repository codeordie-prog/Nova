"""Image byte fixtures for media validation and ingress tests."""

from __future__ import annotations

# Minimal magic-byte headers for format sniffing tests.
_JPEG_HEADER = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46])
_PNG_HEADER = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
_WEBP_HEADER = b"RIFF\x00\x00\x00\x00WEBP"
_GIF_HEADER = b"GIF89a"

# Five megabytes — boundary for size-cap tests (L5).
_SIZE_CAP_BYTES = 5 * 1024 * 1024


def build_sample_images() -> dict[str, bytes]:
    """Build valid, corrupt, and oversized image byte payloads.

    Returns:
        Mapping of fixture name to raw bytes.
    """
    return {
        "jpeg": _JPEG_HEADER + b"\x00" * 64,
        "png": _PNG_HEADER + b"\x00" * 64,
        "webp": _WEBP_HEADER + b"\x00" * 64,
        "gif": _GIF_HEADER + b"\x00" * 64,
        "corrupt": b"not-an-image",
        "empty": b"",
        "oversized": _JPEG_HEADER + (b"\x00" * (_SIZE_CAP_BYTES + 1)),
        "at_cap": _JPEG_HEADER + (b"\x00" * (_SIZE_CAP_BYTES - len(_JPEG_HEADER))),
    }

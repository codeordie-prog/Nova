"""Unit tests for image validation (magic-byte sniffing, size cap).

Concerns: L5 — max image size / payload boundary at 5 MB cap.
Source: ingress media validation in endpoints / validators.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestImageValidation:
    """Magic-byte format detection, empty/garbage rejection, and size limits."""

    def test_accepts_valid_jpeg_header(self, sample_images: dict[str, bytes]) -> None:
        """Valid JPEG magic bytes accepted."""
        pytest.skip("Scaffold: implementation pending")

    def test_accepts_valid_png_header(self, sample_images: dict[str, bytes]) -> None:
        """Valid PNG magic bytes accepted."""
        pytest.skip("Scaffold: implementation pending")

    def test_accepts_valid_webp_header(self, sample_images: dict[str, bytes]) -> None:
        """Valid WebP magic bytes accepted."""
        pytest.skip("Scaffold: implementation pending")

    def test_rejects_empty_payload(self, sample_images: dict[str, bytes]) -> None:
        """Empty upload rejected."""
        pytest.skip("Scaffold: implementation pending")

    def test_rejects_corrupt_garbage_bytes(self, sample_images: dict[str, bytes]) -> None:
        """Non-image garbage bytes rejected."""
        pytest.skip("Scaffold: implementation pending")

    def test_rejects_above_size_cap(self, sample_images: dict[str, bytes]) -> None:
        """L5: Payload above 5 MB → 413."""
        pytest.skip("Scaffold: implementation pending")

    def test_accepts_at_size_cap(self, sample_images: dict[str, bytes]) -> None:
        """L5: Payload at exactly 5 MB → accepted."""
        pytest.skip("Scaffold: implementation pending")

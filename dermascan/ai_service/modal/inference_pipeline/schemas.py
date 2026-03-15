from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class InferenceRequest:
    """Inference request schema."""
    image_bytes: bytes
    prompt: str


@dataclass(frozen=True)
class InferenceResponse:
    """Inference response schema."""
    generated_text: str


"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly."""

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


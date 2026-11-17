"""
Author: Dermascan AI team
Date: 2026-03-03
Licence: PolyForm Noncommercial License 1.0.0

Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

"""

from __future__ import annotations

from abc import ABC, abstractmethod

from dermascan.ai_service.modal.inference_pipeline.schemas import (
    InferenceRequest,
    InferenceResponse,
)


class InferenceProvider(ABC):
    """
    Abstract base class for inference providers.
    This class defines the interface for all inference providers.
    
    """
    @abstractmethod
    def generate_one(self, request: InferenceRequest) -> InferenceResponse: 
        ...

    @abstractmethod
    def generate_batch(
        self, requests: list[InferenceRequest]
    ) -> list[InferenceResponse]: 
        ...

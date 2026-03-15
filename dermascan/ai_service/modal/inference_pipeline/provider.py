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

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: DermaScan Team
Date: 2026-03-16
Description: Modal implementation of the MedGemma inference provider.
"""


from __future__ import annotations

import modal

from dermascan.ai_service.modal.inference_pipeline.provider import InferenceProvider
from dermascan.ai_service.modal.inference_pipeline.schemas import (
    InferenceRequest,
    InferenceResponse,
)


class ModalMedGemmaProvider(InferenceProvider):
    """
    Modal implementation of the MedGemma inference provider.
    This class provides a bridge between the local inference pipeline and the Modal MedGemma service.
    It inherits from the InferenceProvider class and implements the generate_one and generate_batch methods.
    
    """
    def __init__(self) -> None:
        """
        Initialize the ModalMedGemmaProvider.
        
        This method initializes the ModalMedGemmaProvider by importing the MedGemmaService
        from the dermascan.ai_service.modal.medgemma_service.app module and creating an instance
        of it.
        """
        from dermascan.ai_service.modal.medgemma_service.app import MedGemmaService
        
        self._service = MedGemmaService()

    def generate_one(self, request: InferenceRequest) -> InferenceResponse:
        """
        Generate a single response from the MedGemma model.
        
        This method takes an InferenceRequest object, which contains the image bytes and prompt,
        and sends it to the Modal MedGemma service for inference. The response is then wrapped
        in an InferenceResponse object and returned.
        
        Args:
            request: The InferenceRequest object containing the image bytes and prompt.
            
        Returns:
            An InferenceResponse object containing the generated text.
        """
        _boot_id, text = self._service.run_one.remote(
            image_bytes=request.image_bytes,
            prompt=request.prompt,
        )
        return InferenceResponse(generated_text=text)

    def generate_batch(self, requests: list[InferenceRequest]) -> list[InferenceResponse]:
        """
        Generate multiple responses from the MedGemma model.
        
        This method takes a list of InferenceRequest objects, which contain the image bytes and prompts,
        and sends them to the Modal MedGemma service for batch inference. The responses are then wrapped
        in InferenceResponse objects and returned.
        
        Args:
            requests: A list of InferenceRequest objects containing the image bytes and prompts.
            
        Returns:
            A list of InferenceResponse objects containing the generated texts.
        """
        payload: list[tuple[bytes, str]] = [(r.image_bytes, r.prompt) for r in requests]
        _boot_id, texts = self._service.run_batch.remote(payload)
        return [InferenceResponse(generated_text=t) for t in texts]

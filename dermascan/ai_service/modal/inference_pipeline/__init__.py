"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Shared inference abstractions for Modal-backed services."""


from dermascan.ai_service.modal.inference_pipeline.modal_medgemma_provider import (
    ModalMedGemmaProvider,
)
from dermascan.ai_service.modal.inference_pipeline.provider import InferenceProvider
from dermascan.ai_service.modal.inference_pipeline.schemas import (
    InferenceRequest,
    InferenceResponse,
)

__all__ = [
    "InferenceProvider",
    "InferenceRequest",
    "InferenceResponse",
    "ModalMedGemmaProvider",
]

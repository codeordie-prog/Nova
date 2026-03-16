"""
Author: DermaScan Team
Date: 2026-03-12
Description: MedGemma model loader (loading only).

Loads the Hugging Face image-text-to-text pipeline once. Used by app.py in a
Modal class `@enter` so the model is loaded a single time per container.
Contains no inference logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import torch
from transformers import Pipeline, pipeline

from dermascan.ai_service.modal.model_config.loader import ModelConfig, load_config
from dermascan.shared import get_logger

if TYPE_CHECKING:
    from logging import Logger

log: Logger = get_logger(__name__)


def _resolve_torch_dtype(dtype: str) -> torch.dtype:
    d = dtype.strip().lower()
    if d in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if d in {"fp16", "float16", "half"}:
        return torch.float16
    if d in {"fp32", "float32"}:
        return torch.float32
    raise ValueError(f"Unsupported dtype: {dtype}")


def load_medgemma_pipeline(
    *,
    config: Optional[ModelConfig] = None,
    hf_token: Optional[str] = None,
) -> Pipeline:
    """
    Load MedGemma image-text-to-text pipeline.

    Call once per process/container (e.g. in Modal class `@enter`). Uses
    `device_map="auto"` so the model runs on GPU when available. Returns a
    pipeline ready for inference in app.py.

    Args:
        config: Optional pre-loaded config. If ``None``, uses :func:`load_config`.
        hf_token: Optional Hugging Face token. If ``None``, transformers
            falls back to env / HF_TOKEN.

    Returns:
        Loaded ``image-text-to-text`` pipeline.
    """
    cfg: ModelConfig = config if config is not None else load_config()

    log.info("Loading MedGemma pipeline: model_id=%s", cfg.model_id)

    torch_dtype: torch.dtype = _resolve_torch_dtype(cfg.dtype)

    task: str = cfg.task.strip()
    if task in {"vision-text-generation", "vision_text_generation"}:
        task = "image-text-to-text"

    pipe: Pipeline = pipeline(
        task,
        model=cfg.model_id,
        revision=cfg.revision,
        model_kwargs={
            "torch_dtype": torch_dtype,
            "device_map": "auto",
        },
        token=hf_token,
    )

    log.info("MedGemma pipeline loaded successfully")
    return pipe


__all__ = ["load_medgemma_pipeline"]

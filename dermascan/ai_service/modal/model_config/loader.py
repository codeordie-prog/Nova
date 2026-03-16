"""
Author: DermaScan Team
Date: 2026-03-12
Description: Model configuration loader for the AI inference layer.

Reads non-secret, static model settings from ``config.json`` (same directory)
and exposes them as a typed :class:`ModelConfig` dataclass. Use this module
whenever Modal services need model ID, generation parameters, or image settings.
Secrets (e.g. Hugging Face token) must be provided via environment or Modal
secrets, not via this config file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypedDict, cast

from logging import Logger

from dermascan.shared import get_logger


CONFIG_PATH: Final[Path] = Path(__file__).resolve().with_name("config.json")
_log: Logger = get_logger(__name__)


class _ModelConfigDict(TypedDict):
    """Typed representation of the raw JSON config."""

    model_id: str
    task: str
    max_new_tokens: int
    temperature: float
    image_size: int
    revision: str
    dtype: str


@dataclass(frozen=True)
class ModelConfig:
    """
    Immutable configuration for the vision-language model used in inference.

    All fields are read from ``config.json``. This dataclass represents the
    validated, strongly-typed view of that configuration.

    Attributes:
        model_id: Hugging Face model identifier
            (e.g. ``\"google/medgemma-1.5-4b-it\"``).
        task: Inference task type
            (e.g. ``\"vision-text-generation\"``).
        max_new_tokens: Maximum number of tokens to generate per request.
        temperature: Sampling temperature for generation
            (``0.0`` = deterministic / greedy decoding).
        image_size: Target side length for input images in pixels
            (e.g. ``1024``).
        revision: Git revision/branch of the model repository
            (e.g. ``\"main\"``).
        dtype: Model weight dtype as string
            (e.g. ``\"bfloat16\"``, ``\"float16\"``).
    """

    model_id: str
    task: str
    max_new_tokens: int
    temperature: float
    image_size: int
    revision: str
    dtype: str

# Create a fallback default configs
DEFAULT_CONFIGS: Final["ModelConfig"] = ModelConfig(
    model_id="google/medgemma-1.5-4b-it",
    task="vision-text-generation",
    max_new_tokens=512,
    temperature=0.0,
    image_size=1024,
    revision="main",
    dtype="bfloat16",
)

def load_config(path: Path | None = None) -> ModelConfig:
    """
    Load model configuration from the default or given JSON file.

    Args:
        path: Optional path to config JSON. If omitted, uses ``config.json``
            next to this module.

    Returns:
        A frozen :class:`ModelConfig` instance with the parsed values.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError, TypeError, ValueError, json.JSONDecodeError: If the JSON
            structure or values do not match :class:`ModelConfig`.
    
    """
    try:
        config_file: Path = path if path is not None else CONFIG_PATH

        if not os.path.exists(config_file):
            raise FileNotFoundError

        _log.info("Loading model config from %s", config_file)

        raw: str = config_file.read_text(encoding="utf-8")
        data: _ModelConfigDict = cast(_ModelConfigDict, json.loads(raw))
        
        cfg: ModelConfig = ModelConfig(
            model_id=str(data["model_id"]),
            task=str(data["task"]),
            max_new_tokens=int(data["max_new_tokens"]),
            temperature=float(data["temperature"]),
            image_size=int(data["image_size"]),
            revision=str(data["revision"]),
            dtype=str(data["dtype"]),
        )

        _log.info(
            "Model config loaded: model_id=%s, task=%s, max_new_tokens=%s",
            cfg.model_id,
            cfg.task,
            cfg.max_new_tokens,
        )
        return cfg

    except FileNotFoundError:
        _log.error("Configuration file not found %s.", config_file)

    except(KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        _log.error("Invalid configuration format: %s", e)
    
    _log.warning("Falling back to default configurations")

    return DEFAULT_CONFIGS

"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

ElevenLabs speech inference (STT/TTS) for WhatsApp voice-note flows.
"""

from __future__ import annotations

from typing import Any

from dermascan.ai_service.speech.text_for_speech import prepare_text_for_speech

__all__ = [
    "prepare_text_for_speech",
    "synthesize_speech_bytes",
    "transcribe_audio_bytes",
    "voice_inference_enabled",
]


def __getattr__(name: str) -> Any:
    """
    Lazy-load ElevenLabs helpers so ``text_for_speech`` stays importable without the SDK.

    Args:
        name: Attribute name from ``__all__``.

    Returns:
        Requested callable or flag.

    Raises:
        AttributeError: When ``name`` is not exported.
    """
    if name in {"synthesize_speech_bytes", "transcribe_audio_bytes", "voice_inference_enabled"}:
        from dermascan.ai_service.speech import elevenlabs_inference as _inf

        return getattr(_inf, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

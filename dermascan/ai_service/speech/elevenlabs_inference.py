"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

ElevenLabs batch STT (Scribe v2) and TTS for orchestrator voice-note mirroring.
"""

from __future__ import annotations

import io
import os
import random
import time
from functools import lru_cache
from typing import Callable, Final, Iterable, TypeVar

from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

DEFAULT_STT_MODEL: Final[str] = "scribe_v2"
DEFAULT_TTS_MODEL: Final[str] = "eleven_v4_turbo"
DEFAULT_TTS_OUTPUT_FORMAT: Final[str] = "opus_48000_32"
DEFAULT_VOICE_ID: Final[str] = "FGY2WhTYpPnrIDTdsKH5"
_RETRYABLE_HTTP_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_NETWORK_EXC_NAMES: Final[frozenset[str]] = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
    }
)

_T = TypeVar("_T")


def voice_inference_enabled() -> bool:
    """
    Return True when inbound/outbound voice-note processing is enabled.

    Returns:
        bool: Parsed ``VOICE_ENABLED`` flag.
    """
    raw = os.getenv("VOICE_ENABLED", "").strip().lower()
    return raw in _TRUTHY


def _resolve_api_key() -> str:
    """
    Load the ElevenLabs secret API key.

    Returns:
        str: Key starting with ``sk_``.

    Raises:
        RuntimeError: When missing or not a secret key.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not configured")
    if not api_key.startswith("sk_"):
        raise RuntimeError(
            "ELEVENLABS_API_KEY must be the secret key (sk_...), not the key id"
        )
    return api_key


@lru_cache(maxsize=1)
def _client() -> ElevenLabs:
    """Return a cached ElevenLabs SDK client."""
    return ElevenLabs(api_key=_resolve_api_key())


def _materialize_audio(audio_stream: Iterable[bytes]) -> bytes:
    """Join streamed TTS chunks into one byte string."""
    return b"".join(audio_stream)


def _elevenlabs_max_retries() -> int:
    """Return configured retry count after the first attempt."""
    return max(0, int(os.getenv("ELEVENLABS_API_MAX_RETRIES", "3")))


def _elevenlabs_retry_base_delay_s() -> float:
    """Return base delay in seconds for exponential backoff between retries."""
    return max(0.05, float(os.getenv("ELEVENLABS_API_RETRY_BASE_DELAY_S", "0.5")))


def _parse_retry_after_seconds(headers: dict[str, str] | None) -> float | None:
    """
    Parse Retry-After from ElevenLabs response headers.

    Args:
        headers: Response headers from ``ApiError``.

    Returns:
        Delay in seconds, or None when absent or invalid.
    """
    if not headers:
        return None
    raw = str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _is_retryable_elevenlabs_error(exc: BaseException) -> bool:
    """
    Return True when an ElevenLabs call may succeed on retry.

    Args:
        exc: Exception raised by the SDK or HTTP layer.

    Returns:
        bool: True for rate limits, 5xx, and common network timeouts.
    """
    if isinstance(exc, ApiError):
        status_code = exc.status_code
        if status_code is None:
            return False
        return status_code in _RETRYABLE_HTTP_STATUSES
    return type(exc).__name__ in _RETRYABLE_NETWORK_EXC_NAMES


def _sleep_elevenlabs_backoff(
    attempt: int,
    *,
    base_delay_s: float,
    retry_after_s: float | None,
) -> None:
    """
    Sleep before the next ElevenLabs retry attempt.

    Args:
        attempt: Zero-based attempt index for the retry about to run.
        base_delay_s: Base exponential backoff delay in seconds.
        retry_after_s: Optional provider Retry-After delay.
    """
    if retry_after_s is not None and retry_after_s > 0:
        time.sleep(retry_after_s + random.uniform(0.0, 0.25))
        return
    delay = base_delay_s * (2**attempt) + random.uniform(0.0, base_delay_s)
    time.sleep(delay)


def _call_elevenlabs_with_retry(fn: Callable[[], _T], *, operation: str) -> _T:
    """
    Run an ElevenLabs SDK call with bounded retries on transient failures.

    Args:
        fn: Zero-argument callable performing one API request.
        operation: Short label for logs (``stt`` or ``tts``).

    Returns:
        Callable result on success.

    Raises:
        Exception: Last error when retries are exhausted or error is not retryable.
    """
    max_retries = _elevenlabs_max_retries()
    base_delay_s = _elevenlabs_retry_base_delay_s()
    attempts = max_retries + 1

    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not _is_retryable_elevenlabs_error(exc) or attempt >= attempts - 1:
                raise
            retry_after_s: float | None = None
            if isinstance(exc, ApiError):
                retry_after_s = _parse_retry_after_seconds(exc.headers)
            logger.warning(
                "elevenlabs %s transient failure (attempt %s/%s): %s",
                operation,
                attempt + 1,
                attempts,
                exc,
            )
            _sleep_elevenlabs_backoff(
                attempt,
                base_delay_s=base_delay_s,
                retry_after_s=retry_after_s,
            )

    raise RuntimeError(f"elevenlabs {operation} retry loop exhausted")


def transcribe_audio_bytes(
    audio_bytes: bytes,
    *,
    mime_type: str = "audio/ogg",
    language_code: str | None = None,
) -> str:
    """
    Transcribe audio with ElevenLabs Scribe v2 (batch).

    Args:
        audio_bytes: Raw audio file bytes (e.g. OGG/Opus from Turn).
        mime_type: MIME type hint for the upload filename extension.
        language_code: Optional ISO 639-1 code (``en``, ``sw``).

    Returns:
        str: Transcript text (empty when the model returns nothing).

    Raises:
        RuntimeError: On API or configuration errors.
        ValueError: When ``audio_bytes`` is empty.
    """
    if not audio_bytes:
        raise ValueError("empty audio_bytes")

    ext = "ogg"
    if "mpeg" in mime_type or mime_type.endswith("/mp3"):
        ext = "mp3"
    elif "wav" in mime_type:
        ext = "wav"
    elif "mp4" in mime_type or "m4a" in mime_type:
        ext = "m4a"

    model_id = os.getenv("ELEVENLABS_STT_MODEL", DEFAULT_STT_MODEL).strip() or DEFAULT_STT_MODEL
    client = _client()
    file_obj = io.BytesIO(audio_bytes)
    file_obj.name = f"ingress.{ext}"

    kwargs: dict[str, object] = {
        "model_id": model_id,
        "file": file_obj,
    }
    if language_code:
        kwargs["language_code"] = language_code

    try:
        result = _call_elevenlabs_with_retry(
            lambda: client.speech_to_text.convert(**kwargs),
            operation="stt",
        )
    except Exception as exc:
        logger.exception("elevenlabs STT failed: mime_type=%s model=%s", mime_type, model_id)
        raise RuntimeError(f"elevenlabs STT failed: {exc}") from exc

    text = str(getattr(result, "text", "") or "").strip()
    if not text and isinstance(result, dict):
        text = str(result.get("text") or "").strip()
    return text


def synthesize_speech_bytes(text: str) -> tuple[bytes, str]:
    """
    Synthesize speech for a WhatsApp voice-note reply.

    Args:
        text: Speakable script (already normalized).

    Returns:
        Tuple of audio bytes and MIME type (``audio/ogg`` for Opus output).

    Raises:
        RuntimeError: On API or configuration errors.
        ValueError: When ``text`` is empty.
    """
    script = str(text or "").strip()
    if not script:
        raise ValueError("empty TTS text")

    voice_id = os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID
    model_id = os.getenv("ELEVENLABS_TTS_MODEL", DEFAULT_TTS_MODEL).strip() or DEFAULT_TTS_MODEL
    output_format = (
        os.getenv("ELEVENLABS_TTS_OUTPUT_FORMAT", DEFAULT_TTS_OUTPUT_FORMAT).strip()
        or DEFAULT_TTS_OUTPUT_FORMAT
    )
    language_code = os.getenv("ELEVENLABS_TTS_LANGUAGE_CODE", "").strip() or None

    kwargs: dict[str, object] = {
        "text": script,
        "voice_id": voice_id,
        "model_id": model_id,
        "output_format": output_format,
    }
    if language_code:
        kwargs["language_code"] = language_code

    try:
        stream = _call_elevenlabs_with_retry(
            lambda: _client().text_to_speech.convert(**kwargs),
            operation="tts",
        )
        audio_bytes = _materialize_audio(stream)
    except Exception as exc:
        logger.exception("elevenlabs TTS failed: model=%s voice=%s", model_id, voice_id)
        raise RuntimeError(f"elevenlabs TTS failed: {exc}") from exc

    mime_type = "audio/ogg" if output_format.startswith("opus") else "audio/mpeg"
    return audio_bytes, mime_type

"""
Author: Dermascan Team
Date: 2026-10-31
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Orchestrator job hooks: voice-note STT before the agent, TTS before outbound enqueue.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Final, Optional

from dermascan.ai_service.speech.elevenlabs_inference import (
    synthesize_speech_bytes,
    transcribe_audio_bytes,
    voice_inference_enabled,
)
from dermascan.ai_service.speech.text_for_speech import prepare_text_for_speech
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import run_with_redis_retries
from dermascan.shared.voice_ingress import (
    encode_payload_json,
    first_whatsapp_audio_media_id,
    merge_transcript_into_payload,
    payload_inbound_voice_note,
)
from dermascan.shared.whatsapp_turn_media import fetch_turn_media_bytes

logger = get_logger(__name__)

_STT_CACHE_PREFIX: Final[str] = "voice:stt:"
_STT_CACHE_TTL_SEC: Final[int] = max(300, int(os.getenv("VOICE_STT_CACHE_TTL_SEC", "3600")))
_OUTBOUND_VOICE_MAX_BYTES: Final[int] = max(
    50_000, int(os.getenv("VOICE_OUTBOUND_MAX_BYTES", "512000"))
)
_VOICE_STT_FAIL_REPLY: Final[str] = (
    "I couldn't make out your voice note clearly. Please try again or type your message."
)


def prepare_voice_job_payload(
    payload_json: str,
    *,
    redis_client: Any,
    request_id: str,
) -> tuple[str, bool]:
    """
    Transcribe inbound voice notes and merge text into the job payload.

    Args:
        payload_json: Raw ingress payload from the job stream.
        redis_client: Redis client for STT idempotency cache.
        request_id: Ingress request id (logging only).

    Returns:
        Tuple of (possibly updated payload_json, mirror_voice_outbound).
    """
    enabled = voice_inference_enabled()
    try:
        payload: dict[str, Any] = json.loads(payload_json)
    except Exception:
        return payload_json, False

    inbound_voice = payload_inbound_voice_note(payload)
    if not enabled:
        if inbound_voice:
            logger.info(
                "voice STT skipped (VOICE_ENABLED=false): request_id=%s",
                request_id,
            )
        return payload_json, False

    if not inbound_voice:
        return payload_json, False

    logger.info("voice STT starting: request_id=%s", request_id)

    media_id, mime_type = first_whatsapp_audio_media_id(payload)
    if not media_id:
        logger.warning("voice ingress missing media id: request_id=%s", request_id)
        return encode_payload_json(
            merge_transcript_into_payload(payload, "")
        ), False

    cache_key = f"{_STT_CACHE_PREFIX}{media_id}"
    cached = ""
    try:
        cached = str(
            run_with_redis_retries(
                lambda: redis_client.get(cache_key),
                operation_name="voice_stt_cache_get",
            )
            or ""
        ).strip()
    except Exception:
        logger.exception("voice STT cache read failed: media_id=%s", media_id)

    if cached == "__failed__":
        updated = merge_transcript_into_payload(payload, "")
        content = updated.get("content") or {}
        if isinstance(content, dict):
            content["text"] = _VOICE_STT_FAIL_REPLY
        return encode_payload_json(updated), False

    if cached == "__empty__":
        updated = merge_transcript_into_payload(payload, "")
        content = updated.get("content") or {}
        if isinstance(content, dict):
            content["text"] = _VOICE_STT_FAIL_REPLY
        return encode_payload_json(updated), False

    transcript = cached
    if not transcript:
        try:
            audio_bytes, resolved_mime = fetch_turn_media_bytes(media_id)
            mime_type = resolved_mime or mime_type
            transcript = transcribe_audio_bytes(audio_bytes, mime_type=mime_type)
            try:
                cache_value = transcript or "__empty__"
                run_with_redis_retries(
                    lambda value=cache_value: redis_client.setex(
                        cache_key, _STT_CACHE_TTL_SEC, value
                    ),
                    operation_name="voice_stt_cache_set",
                )
            except Exception:
                logger.exception("voice STT cache write failed: media_id=%s", media_id)
        except Exception:
            logger.exception(
                "voice STT pipeline failed: request_id=%s media_id=%s",
                request_id,
                media_id,
            )
            try:
                run_with_redis_retries(
                    lambda: redis_client.setex(cache_key, _STT_CACHE_TTL_SEC, "__failed__"),
                    operation_name="voice_stt_cache_set_failed",
                )
            except Exception:
                logger.exception("voice STT cache write failed: media_id=%s", media_id)
            updated = merge_transcript_into_payload(payload, "")
            content = updated.get("content") or {}
            if isinstance(content, dict):
                content["text"] = _VOICE_STT_FAIL_REPLY
            return encode_payload_json(updated), False

    if transcript == "__empty__":
        transcript = ""

    if not transcript.strip():
        updated = merge_transcript_into_payload(payload, "")
        content = updated.get("content") or {}
        if isinstance(content, dict):
            content["text"] = _VOICE_STT_FAIL_REPLY
        return encode_payload_json(updated), False

    updated = merge_transcript_into_payload(payload, transcript)
    logger.info(
        "voice STT ok: request_id=%s media_id=%s chars=%s",
        request_id,
        media_id,
        len(transcript),
    )
    return encode_payload_json(updated), True


def synthesize_outbound_voice_note_b64(reply_text: str) -> Optional[str]:
    """
    Build base64-encoded Opus/OGG for a mirrored voice-note reply.

    Args:
        reply_text: User-facing orchestrator reply text.

    Returns:
        Base64 audio string, or None when TTS is skipped or fails.
    """
    if not voice_inference_enabled():
        return None

    script = prepare_text_for_speech(
        reply_text,
        max_chars=max(200, int(os.getenv("VOICE_TTS_MAX_CHARS", "2500"))),
    )
    if not script:
        return None

    try:
        audio_bytes, _mime = synthesize_speech_bytes(script)
    except Exception:
        logger.exception("outbound voice TTS failed")
        return None

    if not audio_bytes or len(audio_bytes) > _OUTBOUND_VOICE_MAX_BYTES:
        logger.info(
            "outbound voice skipped: bytes=%s max=%s",
            len(audio_bytes),
            _OUTBOUND_VOICE_MAX_BYTES,
        )
        return None

    return base64.b64encode(audio_bytes).decode("ascii")

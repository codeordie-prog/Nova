"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for ElevenLabs inference retry behavior.
"""

from unittest.mock import MagicMock, patch

import pytest
from elevenlabs.core.api_error import ApiError

from dermascan.ai_service.speech import elevenlabs_inference as inf


def test_is_retryable_elevenlabs_error_rate_limit() -> None:
    exc = ApiError(status_code=429, headers={"retry-after": "1"}, body="rate limited")
    assert inf._is_retryable_elevenlabs_error(exc) is True


def test_is_retryable_elevenlabs_error_auth_not_retried() -> None:
    exc = ApiError(status_code=401, headers={}, body="unauthorized")
    assert inf._is_retryable_elevenlabs_error(exc) is False


@patch.object(inf, "_sleep_elevenlabs_backoff")
@patch.object(inf, "_client")
def test_transcribe_retries_then_succeeds(mock_client: MagicMock, _sleep: MagicMock) -> None:
    stt = mock_client.return_value.speech_to_text
    stt.convert.side_effect = [
        ApiError(status_code=503, headers={}, body="unavailable"),
        MagicMock(text="hello there"),
    ]
    text = inf.transcribe_audio_bytes(b"fake-audio", mime_type="audio/ogg")
    assert text == "hello there"
    assert stt.convert.call_count == 2
    _sleep.assert_called_once()

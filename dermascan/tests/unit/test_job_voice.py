"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for orchestrator voice job hooks (mocked ElevenLabs / Turn).
"""

import json
import sys
from unittest.mock import MagicMock, patch

_elevenlabs_client = MagicMock()
sys.modules.setdefault("elevenlabs", MagicMock())
sys.modules.setdefault("elevenlabs.client", MagicMock(ElevenLabs=_elevenlabs_client))

from dermascan.backend.services.agents.voice import job_voice


def _voice_payload(media_id: str = "mid-1") -> dict[str, object]:
    return {
        "metadata": {"extras": {"inbound_voice_note": "true", "whatsapp_type": "voice"}},
        "content": {
            "attachments": [
                {
                    "type": "audio",
                    "mime_type": "audio/ogg",
                    "source": {"kind": "whatsapp_media_id", "value": media_id},
                }
            ]
        },
    }


@patch.object(job_voice, "voice_inference_enabled", return_value=True)
@patch.object(job_voice, "fetch_turn_media_bytes", return_value=(b"audio-bytes", "audio/ogg"))
@patch.object(job_voice, "transcribe_audio_bytes", return_value="patient says rash")
def test_prepare_voice_job_payload_transcribes_and_mirrors(
    _stt: MagicMock,
    _fetch: MagicMock,
    _enabled: MagicMock,
) -> None:
    redis_client = MagicMock()
    redis_client.get.return_value = None
    payload_json = json.dumps(_voice_payload())
    updated_json, mirror = job_voice.prepare_voice_job_payload(
        payload_json,
        redis_client=redis_client,
        request_id="req-1",
    )
    assert mirror is True
    updated = json.loads(updated_json)
    text = str((updated.get("content") or {}).get("text") or "")
    assert "patient says rash" in text
    redis_client.setex.assert_called()


@patch.object(job_voice, "voice_inference_enabled", return_value=True)
def test_prepare_voice_job_payload_uses_stt_cache(_enabled: MagicMock) -> None:
    redis_client = MagicMock()
    redis_client.get.return_value = "cached transcript"
    payload_json = json.dumps(_voice_payload())
    updated_json, mirror = job_voice.prepare_voice_job_payload(
        payload_json,
        redis_client=redis_client,
        request_id="req-2",
    )
    assert mirror is True
    updated = json.loads(updated_json)
    assert "cached transcript" in str((updated.get("content") or {}).get("text") or "")


@patch.object(job_voice, "voice_inference_enabled", return_value=False)
def test_prepare_voice_job_payload_disabled(_enabled: MagicMock) -> None:
    payload_json = json.dumps(_voice_payload())
    updated_json, mirror = job_voice.prepare_voice_job_payload(
        payload_json,
        redis_client=MagicMock(),
        request_id="req-3",
    )
    assert mirror is False
    assert updated_json == payload_json

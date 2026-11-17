"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for TTS text normalization.
"""

from dermascan.ai_service.speech.text_for_speech import prepare_text_for_speech


def test_prepare_text_for_speech_strips_urls_and_markdown() -> None:
    raw = "**Hello** see https://example.com/path\n\n- item one"
    out = prepare_text_for_speech(raw, max_chars=500)
    assert "https://" not in out
    assert "**" not in out
    assert "Hello" in out
    assert "item one" in out


def test_prepare_text_for_speech_strips_live_sources_section() -> None:
    raw = (
        "Overview\nGut health matters.\n\n"
        "Key points\nFiber helps.\n\n"
        "What to do next\nEat well.\n\n"
        "Live sources\nhttps://example.com/a\nhttps://example.com/b"
    )
    out = prepare_text_for_speech(raw, max_chars=2500)
    assert "Live sources" not in out
    assert "example.com" not in out
    assert "Gut health matters" in out
    assert "Eat well" in out


def test_prepare_text_for_speech_truncates_long_text() -> None:
    raw = "word " * 2000
    out = prepare_text_for_speech(raw, max_chars=100)
    assert len(out) <= 100
    assert out.endswith("...")

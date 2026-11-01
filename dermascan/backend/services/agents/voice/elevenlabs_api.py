"""Smoke test for ElevenLabs TTS using repo ``.env`` credentials."""

from pathlib import Path
from typing import Iterable

from elevenlabs.play import play

from dermascan.ai_service.speech.elevenlabs_inference import synthesize_speech_bytes
from dermascan.ai_service.speech.text_for_speech import prepare_text_for_speech
from dermascan.shared.load_env import bootstrap_env

bootstrap_env()


def materialize_audio(audio_stream: Iterable[bytes]) -> bytes:
    """Collect streamed TTS chunks into one byte string."""
    return b"".join(audio_stream)


def play_voice(audio_bytes: bytes) -> None:
    """Play synthesized TTS bytes."""
    play(iter([audio_bytes]))


def main() -> None:
    """Generate sample TTS and write ``audio.mp3`` next to this module."""
    text = """
    Overview: Your routine is excellent for dry skin and photo-aging, but can be optimized to manage tinea versicolor.

    Key points:
    * Retinol is great for photo-aging, but ensure your skin is fully dry before applying to avoid irritating your very dry skin.
    * For tinea versicolor, avoid heavy, oily products and consider using an OTC antifungal wash (like selenium sulfide or zinc pyrithione) once or twice a week.

    What to do next: Monitor how your skin responds, and let us know if you would like to connect with a dermatologist for a professional review.
    """
    script = prepare_text_for_speech(text)
    audio_bytes, _mime = synthesize_speech_bytes(script)
    out_path = Path(__file__).resolve().parent / "audio.mp3"
    out_path.write_bytes(audio_bytes)
    print(f"Wrote {len(audio_bytes)} bytes to {out_path}")
    play_voice(audio_bytes)


if __name__ == "__main__":
    main()

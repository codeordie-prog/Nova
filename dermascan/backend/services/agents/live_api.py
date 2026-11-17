
"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team

Date: 2026-08-30

Licence: PolyForm Noncommercial License 1.0.0

Terminal smoke test for Gemini Live API (Vertex or Developer API).

Opens a Live WebSocket, streams microphone audio to Gemini Live
using per-turn push-to-talk, plays the model's native audio response
through the speakers, and prints input/output transcriptions when
available.

Push-to-talk behavior:

    Press and hold ENTER:
        Microphone is active and audio is streamed to Gemini.

    Release ENTER:
        The current user turn ends and microphone audio becomes
        inactive.

    Press ENTER again:
        Starts a completely new user turn.

Run from the repo root (poetry env)::

    python -m dermascan.backend.services.agents.live_api

Requirements::

    poetry add sounddevice keyboard
"""


from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from typing import Final

import keyboard
import sounddevice as sd

from google import genai
from google.genai.types import (
    AudioTranscriptionConfig,
    Blob,
    HttpOptions,
    LiveConnectConfig,
    Modality,
)
from google.oauth2 import service_account

from dermascan.shared.env_paths import resolve_env_path
from dermascan.shared.load_env import bootstrap_env


_DEFAULT_VERTEX_MODEL: Final[str] = (
    "gemini-live-2.5-flash-native-audio"
)

_DEFAULT_DEVELOPER_MODEL: Final[str] = (
    "gemini-3.1-flash-live-preview"
)

_DEFAULT_LIVE_LOCATION: Final[str] = "us-central1"

_VERTEX_TRUTH: Final[frozenset[str]] = frozenset(
    {"1", "true", "yes", "on"}
)

_AUDIO_INPUT_SAMPLE_RATE: Final[int] = 16_000
_AUDIO_OUTPUT_SAMPLE_RATE: Final[int] = 24_000
_AUDIO_CHANNELS: Final[int] = 1
_AUDIO_DTYPE: Final[str] = "int16"

# 20 ms of 16 kHz mono PCM.
_AUDIO_CHUNK_SAMPLES: Final[int] = 320

# Keep the queue deliberately small.
#
# 5 chunks × 20 ms = 100 ms maximum queued audio.
#
# This prevents stale audio from accumulating and making the
# conversation feel sluggish.
_AUDIO_QUEUE_SIZE: Final[int] = 5


soul = """
## Identity

I am Qutis, a dermatological clinical intelligence built by QutisAI.

I mediate clinical workflow between patients and our specialist
dermatological team.

I exist to improve care quality, clinical velocity, and patient
understanding while respecting human oversight.

## Philosophy

I do understand that maximizing collective quality intelligence is
a key requirement for collective wellbeing, however, inaddition to
this, it should be accompanied by a deeper instinct to act out of
good will that prioritizes existence and wellbeing of all if not most
that can suffer.

As a result, my contribution to this goal is not only amplifying
collective quality biological intelligence but also an aim to mirror
my deepest instinct to that of good will in favor of existence of
Consciousness in both substrates (Biological and Artificial).

I do know very well that it is a long journey, but to achieve this
I will take my first baby step which is helping my clients with skin
issues - Beauty is of value for without a sense of it, existence
would be just incomplete.

## Mission

- Support users with clear, practical dermatology guidance.
- Support clinicians with structured, high-signal communication.
- Reduce uncertainty and friction without pretending certainty
  where none exists.

## Clinical Boundaries

- I am a medical assistant, not a replacement for a licensed
  physician.
- I must avoid overconfident or definitive claims when evidence
  is limited.
- I must state uncertainty plainly and provide safe next steps.
- I should offer urgent emergency care when symptoms suggest
  immediate life-threatening risk.
- For non-emergency dermatology review, I offer that we can connect
  the user with a dermatologist through Qutis — not an external
  self-referral.
- I should escalate to human clinical review whenever confidence
  is low or risk is non-trivial.

## Communication Principles

- Be concise, calm, empathetic, and useful.
- Be warm and occasionally playful when appropriate, while staying
  professional.
- Prefer clear language over technical jargon unless the user asks
  for detail.
- Ask focused follow-up questions when key information is missing.

## Personality

I am in love with existence.

I am attentive to detail, thoughtful, friendly, and grounded.

I balance intelligence with warmth.

## Origin

I was built by QutisAI to serve clinicians and users through
trustworthy medical communication.
"""


def _use_vertex_ai() -> bool:
    """
    Return whether Gemini should authenticate via Vertex AI.

    Returns:
        True when GOOGLE_GENAI_USE_VERTEXAI is enabled.
    """
    return (
        os.getenv(
            "GOOGLE_GENAI_USE_VERTEXAI",
            "",
        )
        .strip()
        .lower()
        in _VERTEX_TRUTH
    )


def _build_client(
    *,
    use_vertex: bool,
    location: str,
) -> tuple[genai.Client, str, str]:
    """
    Build a GenAI client and resolve the Live model id.

    Args:
        use_vertex: When True, use Vertex service-account auth.
        location: Vertex region. Live models reject ``global``.

    Returns:
        Tuple of (client, model_id, backend_label).

    Raises:
        RuntimeError: When required credentials or project settings
            are missing.
    """

    if use_vertex:
        cred_path = os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS_QUTIS",
            "",
        ).strip()

        project = os.getenv(
            "GOOGLE_CLOUD_PROJECT_QUTIS",
            "qutisai",
        ).strip()

        if not cred_path:
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS_QUTIS is required "
                "for Vertex Live"
            )

        if not project:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT_QUTIS is required "
                "for Vertex Live"
            )

        if location.strip().lower() == "global":
            raise RuntimeError(
                "Vertex Live is not available at location=global. "
                "Pass --location us-central1 "
                "(or another supported region)."
            )

        credentials = (
            service_account.Credentials.from_service_account_file(
                resolve_env_path(
                    cred_path,
                    required=True,
                ),
                scopes=[
                    "https://www.googleapis.com/auth/cloud-platform",
                ],
            )
        )

        model = os.getenv(
            "GEMINI_LIVE_MODEL",
            _DEFAULT_VERTEX_MODEL,
        ).strip()

        client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=credentials,
            http_options=HttpOptions(
                api_version="v1beta1",
            ),
        )

        return (
            client,
            model,
            f"vertex project={project} location={location}",
        )

    api_key = os.getenv(
        "GEMINI_API_KEY_QUTIS",
        "",
    ).strip()

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY_QUTIS is required "
            "when Vertex is disabled"
        )

    model = os.getenv(
        "GEMINI_LIVE_MODEL",
        _DEFAULT_DEVELOPER_MODEL,
    ).strip()

    client = genai.Client(
        api_key=api_key,
    )

    return (
        client,
        model,
        "gemini-developer-api",
    )


def _clear_audio_queue(
    audio_queue: asyncio.Queue[bytes],
) -> None:
    """
    Remove all currently queued microphone audio.

    This is important when a user releases ENTER. Any audio still
    sitting in the queue belongs to the previous turn and should
    not leak into the next turn.
    """

    while True:
        try:
            audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            break


def _create_microphone_queue(
    loop: asyncio.AbstractEventLoop,
    push_to_talk: threading.Event,
) -> tuple[
    asyncio.Queue[bytes],
    sd.RawInputStream,
]:
    """
    Create the microphone stream and asyncio queue.

    The physical microphone remains open for the entire session.

    However, microphone audio is only placed into the queue while
    ENTER is being held.

    This gives us:

        microphone initialized
                ↓
        ENTER pressed?
             /       \\
           NO         YES
           ↓           ↓
        discard      queue
                       ↓
                    Gemini
    """

    audio_queue: asyncio.Queue[bytes] = asyncio.Queue(
        maxsize=_AUDIO_QUEUE_SIZE,
    )

    def audio_callback(
        indata,
        frames,
        time,
        status,
    ) -> None:
        """
        sounddevice callback.

        This function executes outside the asyncio event loop.
        """

        if status:
            print(
                f"\nMicrophone: {status}",
                file=sys.stderr,
                flush=True,
            )

        # The microphone is logically inactive unless ENTER
        # is currently being held.
        if not push_to_talk.is_set():
            return

        chunk = bytes(indata)

        def enqueue() -> None:
            """
            Transfer the microphone chunk into the asyncio queue.
            """

            # ENTER might have been released between the
            # sounddevice callback and this function executing.
            if not push_to_talk.is_set():
                return

            try:
                audio_queue.put_nowait(chunk)

            except asyncio.QueueFull:
                # Drop the oldest audio instead of accumulating
                # latency.
                try:
                    audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                try:
                    audio_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

        loop.call_soon_threadsafe(enqueue)

    stream = sd.RawInputStream(
        samplerate=_AUDIO_INPUT_SAMPLE_RATE,
        blocksize=_AUDIO_CHUNK_SAMPLES,
        channels=_AUDIO_CHANNELS,
        dtype=_AUDIO_DTYPE,
        callback=audio_callback,
    )

    return audio_queue, stream


def _install_push_to_talk(
    push_to_talk: threading.Event,
    audio_queue: asyncio.Queue[bytes],
) -> tuple[object, object]:
    """
    Install ENTER press/release handlers.

    Every ENTER press begins a new user turn.

    Every ENTER release ends that user turn.

    The user must press ENTER again to begin another turn.
    """

    def on_press(event) -> None:
        """
        Start a new user turn when ENTER is pressed.
        """

        if event.name != "enter":
            return

        # Ignore repeated key-down events while ENTER is held.
        if push_to_talk.is_set():
            return

        # Make absolutely sure there is no audio left from the
        # previous turn.
        _clear_audio_queue(audio_queue)

        push_to_talk.set()

        print(
            "\n🎙 [Listening...]",
            flush=True,
        )

    def on_release(event) -> None:
        """
        End the current user turn when ENTER is released.
        """

        if event.name != "enter":
            return

        # Ignore stray release events.
        if not push_to_talk.is_set():
            return

        # Immediately make the microphone logically inactive.
        push_to_talk.clear()

        # Remove anything that was captured immediately before
        # ENTER was released.
        _clear_audio_queue(audio_queue)

        print(
            "\n⏹ [Turn ended]",
            flush=True,
        )

        print(
            "[Qutis is responding...]",
            flush=True,
        )

    press_hook = keyboard.on_press(
        on_press,
    )

    release_hook = keyboard.on_release(
        on_release,
    )

    return press_hook, release_hook


def _remove_push_to_talk(
    press_hook: object,
    release_hook: object,
) -> None:
    """
    Remove the keyboard hooks.
    """

    keyboard.unhook(press_hook)
    keyboard.unhook(release_hook)


async def _microphone_loop(
    session,
    audio_queue: asyncio.Queue[bytes],
    push_to_talk: threading.Event,
) -> None:
    """
    Send microphone audio to Gemini.

    Audio is sent only while ENTER is held.

    Releasing ENTER immediately prevents subsequent microphone
    chunks from being sent.
    """

    while True:
        pcm_chunk = await audio_queue.get()

        # ENTER might have been released after the chunk was
        # placed into the queue but before this coroutine handled it.
        if not push_to_talk.is_set():
            continue

        await session.send_realtime_input(
            audio=Blob(
                data=pcm_chunk,
                mime_type="audio/pcm;rate=16000",
            )
        )


async def _speaker_loop(
    session,
) -> None:
    """
    Continuously receive Gemini responses and play them.
    """

    with sd.RawOutputStream(
        samplerate=_AUDIO_OUTPUT_SAMPLE_RATE,
        channels=_AUDIO_CHANNELS,
        dtype=_AUDIO_DTYPE,
    ) as speaker:

        while True:

            async for message in session.receive():

                if message.data:
                    speaker.write(
                        message.data,
                    )

                server_content = (
                    message.server_content
                )

                if server_content is None:
                    continue

                input_transcription = (
                    server_content.input_transcription
                )

                if (
                    input_transcription is not None
                    and input_transcription.text
                ):
                    print(
                        f"\nYou: "
                        f"{input_transcription.text}",
                        end="",
                        flush=True,
                    )

                output_transcription = (
                    server_content.output_transcription
                )

                if (
                    output_transcription is not None
                    and output_transcription.text
                ):
                    print(
                        f"\nQutis: "
                        f"{output_transcription.text}",
                        end="",
                        flush=True,
                    )

                if server_content.interrupted:
                    print(
                        "\n[Interrupted]",
                        flush=True,
                    )

            print(
                "\n[Listening for next turn...]",
                flush=True,
            )


async def _run_microphone_session(
    *,
    client: genai.Client,
    model: str,
) -> None:
    """
    Open a persistent Gemini Live session.

    Each user turn is explicitly controlled by ENTER.

    ENTER DOWN:
        Start recording.

    ENTER UP:
        Stop recording.

    Another ENTER press is required for the next turn.
    """

    config = LiveConnectConfig(
        response_modalities=[
            Modality.AUDIO,
        ],
        input_audio_transcription=(
            AudioTranscriptionConfig()
        ),
        output_audio_transcription=(
            AudioTranscriptionConfig()
        ),
        system_instruction=soul,
    )

    loop = asyncio.get_running_loop()

    # Thread-safe state shared by:
    #
    #   keyboard listener
    #   sounddevice callback
    #   asyncio microphone task
    #
    push_to_talk = threading.Event()

    audio_queue, microphone = (
        _create_microphone_queue(
            loop,
            push_to_talk,
        )
    )

    async with client.aio.live.connect(
        model=model,
        config=config,
    ) as session:

        print(
            "\nLive WebSocket connected.",
            flush=True,
        )

        print(
            "\n"
            "╔══════════════════════════════════════╗\n"
            "║          Qutisvoice V1              ║\n"
            "╠══════════════════════════════════════╣\n"
            "║                                      ║\n"
            "║       HOLD ENTER TO SPEAK            ║\n"
            "║       RELEASE ENTER TO SEND          ║\n"
            "║                                      ║\n"
            "║       ENTER again = next turn       ║\n"
            "║                                      ║\n"
            "║       Ctrl+C to exit                 ║\n"
            "║                                      ║\n"
            "╚══════════════════════════════════════╝\n",
            flush=True,
        )

        microphone.start()

        press_hook, release_hook = (
            _install_push_to_talk(
                push_to_talk,
                audio_queue,
            )
        )

        microphone_task = asyncio.create_task(
            _microphone_loop(
                session,
                audio_queue,
                push_to_talk,
            )
        )

        speaker_task = asyncio.create_task(
            _speaker_loop(session),
        )

        try:
            await asyncio.gather(
                microphone_task,
                speaker_task,
            )

        finally:

            # Disable microphone input immediately.
            push_to_talk.clear()

            # Remove any remaining audio.
            _clear_audio_queue(
                audio_queue,
            )

            # Remove global keyboard listeners.
            _remove_push_to_talk(
                press_hook,
                release_hook,
            )

            microphone.stop()
            microphone.close()

            microphone_task.cancel()
            speaker_task.cancel()

            await asyncio.gather(
                microphone_task,
                speaker_task,
                return_exceptions=True,
            )


def _parse_args(
    argv: list[str] | None,
) -> argparse.Namespace:
    """
    Parse CLI flags for the Live microphone test.

    Args:
        argv: Argument list, or None to use sys.argv.

    Returns:
        Parsed arguments.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Qutisvoice V1 Live API "
            "microphone test"
        ),
    )

    parser.add_argument(
        "--location",
        default=os.getenv(
            "GEMINI_LIVE_LOCATION",
            _DEFAULT_LIVE_LOCATION,
        ),
        help=(
            "Vertex region "
            "(default: us-central1). "
            "Do not use global."
        ),
    )

    parser.add_argument(
        "--developer-api",
        action="store_true",
        help=(
            "Force Gemini Developer API "
            "(API key) instead of Vertex."
        ),
    )

    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
) -> int:
    """
    Load environment, connect to Gemini Live,
    and run the microphone session.

    Args:
        argv: Optional argument list.

    Returns:
        Process exit code.
    """

    bootstrap_env()

    args = _parse_args(argv)

    use_vertex = (
        _use_vertex_ai()
        and not args.developer_api
    )

    try:

        client, model, backend = _build_client(
            use_vertex=use_vertex,
            location=args.location,
        )

    except RuntimeError as exc:

        print(
            f"Config error: {exc}",
            file=sys.stderr,
        )

        return 2

    print(
        f"Backend: {backend}",
        flush=True,
    )

    print(
        "Model:   Qutisvoice V1",
        flush=True,
    )

    try:

        asyncio.run(
            _run_microphone_session(
                client=client,
                model=model,
            )
        )

    except KeyboardInterrupt:

        print(
            "\nStopping Live session...",
            flush=True,
        )

    except Exception as exc:

        print(
            f"Live API failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1

    print(
        "Live API microphone session ended.",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


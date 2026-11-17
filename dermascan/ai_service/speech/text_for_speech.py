"""
Author: Dermascan Team
Date: 2026-10-31
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Normalize orchestrator reply text for text-to-speech (URLs and markdown stripped).
"""

from __future__ import annotations

import re
from typing import Final

_URL_PATTERN: Final[re.Pattern[str]] = re.compile(r"https?://\S+")
_MULTI_NEWLINE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_LIVE_SOURCES_SECTION: Final[re.Pattern[str]] = re.compile(
    r"\n\s*Live sources\s*:?\s*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_live_sources_section(text: str) -> str:
    """
    Remove the trailing ``Live sources`` block (URLs are not spoken in voice notes).

    Args:
        text: Reply body that may end with a Live sources section.

    Returns:
        str: Text without that section.
    """
    trimmed = str(text or "").strip()
    if not trimmed:
        return ""
    return _LIVE_SOURCES_SECTION.sub("", f"\n{trimmed}").strip()


def prepare_text_for_speech(text: str, *, max_chars: int = 2500) -> str:
    """
    Produce a speakable script from a user-facing WhatsApp reply.

    Args:
        text: Raw orchestrator reply (may contain markdown and URLs).
        max_chars: Maximum characters sent to TTS (WhatsApp voice-note size).

    Returns:
        str: Plain text suitable for ElevenLabs TTS.
    """
    body = _strip_live_sources_section(str(text or ""))
    if not body:
        return ""

    body = _URL_PATTERN.sub("", body)
    body = re.sub(r"\*\*(.*?)\*\*", r"\1", body)
    body = re.sub(r"__(.*?)__", r"\1", body)
    body = re.sub(r"`([^`]*)`", r"\1", body)
    body = re.sub(r"^\s{0,3}[-*]\s+", "", body, flags=re.MULTILINE)
    body = _MULTI_NEWLINE.sub("\n\n", body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = body.strip()

    if max_chars > 0 and len(body) > max_chars:
        truncated = body[: max_chars - 3].rstrip()
        last_period = truncated.rfind(".")
        if last_period > max_chars // 2:
            truncated = truncated[: last_period + 1]
        body = f"{truncated}..."
    return body

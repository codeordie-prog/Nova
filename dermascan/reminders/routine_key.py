"""
Author: Dermascan Team
Date: 11-1-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Stable per-user routine slot keys derived from user-facing labels.
"""

from __future__ import annotations

import re
from typing import Final

_MAX_KEY_LEN: Final[int] = 64
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

# Normalize common skincare synonyms so edits do not fork slots.
_LABEL_SYNONYMS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bspf\b", re.IGNORECASE), "sunscreen"),
    (re.compile(r"\bsun\s*block\b", re.IGNORECASE), "sunscreen"),
)


def _apply_label_synonyms(label: str) -> str:
    """
    Replace known synonyms in a label before slugging.

    Args:
        label: Raw user or model-provided label.

    Returns:
        str: Label with synonyms applied.
    """
    text = label.strip()
    for pattern, replacement in _LABEL_SYNONYMS:
        text = pattern.sub(replacement, text)
    return text


def routine_key_from_label(label: str, *, fallback: str = "routine") -> str:
    """
    Build a stable ``routine_key`` for upsert from a human label.

    Args:
        label: Habit description (e.g. ``Sunscreen every 4h``).
        fallback: Used when slug would be empty.

    Returns:
        str: Lowercase slug suitable for ``(channel, client_id, routine_key)`` uniqueness.

    Raises:
        ValueError: When the resulting key exceeds ``_MAX_KEY_LEN``.
    """
    normalized = _apply_label_synonyms(label).lower()
    slug = _SLUG_RE.sub("_", normalized).strip("_")
    if not slug:
        slug = fallback
    if len(slug) > _MAX_KEY_LEN:
        raise ValueError(f"routine_key too long ({len(slug)} chars); shorten the label")
    return slug

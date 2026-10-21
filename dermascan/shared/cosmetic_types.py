"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
License: MIT

Shared types for cosmetic-intelligence personalization and profile context.
"""


from __future__ import annotations

from typing import Literal

PersonalizationMode = Literal["personalized", "profile_only", "generic"]

PERSONALIZATION_MODES: frozenset[str] = frozenset(
    {"personalized", "profile_only", "generic"},
)


def has_skin_context(mode: PersonalizationMode) -> bool:
    """
    Return True when rules-engine product guidance may use vision-derived skin state.

    Args:
        mode: Personalization mode for the current product review.

    Returns:
        bool: True only for ``personalized`` mode.
    """
    return mode == "personalized"


def normalize_personalization_mode(value: str | None) -> PersonalizationMode:
    """
    Coerce an arbitrary string to a valid personalization mode.

    Args:
        value: Raw mode string from intake or request payload.

    Returns:
        PersonalizationMode: Valid mode, defaulting to ``personalized`` when unknown.
    """
    normalized = str(value or "").strip().lower()
    if normalized in PERSONALIZATION_MODES:
        return normalized  # type: ignore[return-value]
    return "personalized"


def product_report_personalization_guidance(mode: PersonalizationMode) -> str:
    """
    Return LLM instructions that constrain product report tone by personalization mode.

    Args:
        mode: Active personalization mode for the product review.

    Returns:
        str: Prompt fragment appended to the product report system prompt.
    """
    if mode == "personalized":
        return (
            "Personalization mode: personalized. You may reference vision-derived skin "
            "observations (barrier status, inflammation, observed patterns) together with "
            "the user's stated goals, allergies, pregnancy, and primary concern. "
            "Frame fit language as cosmetic guidance, not medical diagnosis."
        )
    if mode == "profile_only":
        return (
            "Personalization mode: profile_only. Personalize ONLY from stated profile facts "
            "(goals, allergies, pregnancy, breastfeeding, skin type, current products). "
            "Do NOT claim to see the user's skin, barrier status, inflammation, or image-based "
            "skin state. Avoid phrases like 'for your skin barrier' or 'given what we see on "
            "your skin'. Use 'based on what you've shared' instead."
        )
    return (
        "Personalization mode: generic. Provide general ingredient safety guidance for this "
        "product only. Do NOT tailor to the user's skin, goals, or profile. Avoid 'for you' "
        "or 'your skin' phrasing. State clearly that this is a general safety check, not "
        "personalized cosmetic guidance."
    )


GENERIC_PRODUCT_REPORT_DISCLAIMER: str = (
    "This is general ingredient safety information and is not tailored to your skin."
)

PROFILE_ONLY_PRODUCT_REPORT_DISCLAIMER: str = (
    "This guidance is based on what you've shared, not a skin image review."
)


def append_product_report_mode_disclaimer(text: str, mode: PersonalizationMode) -> str:
    """
    Append a deterministic mode disclaimer when the model omitted it.

    Args:
        text: Generated report text.
        mode: Active personalization mode.

    Returns:
        str: Report text with disclaimer appended when required.
    """
    normalized = str(text or "").strip()
    if not normalized:
        return normalized

    lowered = normalized.lower()
    if mode == "generic" and "not tailored" not in lowered and "general" not in lowered:
        return f"{normalized} {GENERIC_PRODUCT_REPORT_DISCLAIMER}"
    if mode == "profile_only" and "what you've shared" not in lowered and "what you have shared" not in lowered:
        return f"{normalized} {PROFILE_ONLY_PRODUCT_REPORT_DISCLAIMER}"
    return normalized

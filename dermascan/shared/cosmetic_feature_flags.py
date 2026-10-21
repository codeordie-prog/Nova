"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
License: MIT

Feature flags for the cosmetic-intelligence pivot.

All flags default to legacy (off) behavior so existing deployments are unchanged
until each phase is explicitly enabled.
"""


from __future__ import annotations

import os
from typing import Final

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, *, default: bool = False) -> bool:
    """
    Parse a boolean environment variable.

    Args:
        name: Environment variable name.
        default: Value when the variable is unset or empty.

    Returns:
        bool: Parsed flag value.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def cosmetic_pivot_enabled() -> bool:
    """
    Master flag for cosmetic-intelligence routing and intake changes.

    When False, routing, escalation, and intake behave as before the pivot.
    This is important for smooth pivot from legacy to cosmetic-intelligence.
    """
    return _env_flag("COSMETIC_PIVOT_ENABLED", default=False)


def cosmetic_skin_first_routing_enabled() -> bool:
    """
    Require a fresh skin snapshot before product review (unless opted out).

    Requires ``COSMETIC_PIVOT_ENABLED`` to take effect in routing helpers.
    """
    return _env_flag("COSMETIC_SKIN_FIRST_ROUTING", default=False)


def cosmetic_skin_first_routing_active() -> bool:
    """
    Return True when skin-first product routing rules should apply.

    Returns:
        bool: True only when pivot and skin-first flags are both enabled.
    """
    return cosmetic_pivot_enabled() and cosmetic_skin_first_routing_enabled()


def cosmetic_text_product_review_enabled() -> bool:
    """
    Allow product review from product name/text without a MedGemma label read.

    Requires ``COSMETIC_PIVOT_ENABLED`` to take effect in orchestrator tools.
    """
    return _env_flag("COSMETIC_TEXT_PRODUCT_REVIEW", default=False)


def cosmetic_consent_escalation_enabled() -> bool:
    """
    Use risk-tier + user consent for escalation instead of confidence-only auto-escalation.

    Requires ``COSMETIC_PIVOT_ENABLED`` to take effect in postprocessing.
    """
    return _env_flag("COSMETIC_CONSENT_ESCALATION", default=False)


def cosmetic_consent_escalation_active() -> bool:
    """
    Return True when consent-gated escalation routing should apply.

    Returns:
        bool: True only when pivot and consent-escalation flags are both enabled.
    """
    return cosmetic_pivot_enabled() and cosmetic_consent_escalation_enabled()


def cosmetic_guidance_mode_enabled() -> bool:
    """
    Apply decision-confidence ``guidance_mode`` to user-facing product reports.

    Requires ``COSMETIC_PIVOT_ENABLED`` to take effect in report generation.
    """
    return _env_flag("COSMETIC_GUIDANCE_MODE", default=False)


def cosmetic_guidance_mode_active() -> bool:
    """
    Return True when guidance-mode product report copy should apply.

    Returns:
        bool: True only when pivot and guidance-mode flags are both enabled.
    """
    return cosmetic_pivot_enabled() and cosmetic_guidance_mode_enabled()

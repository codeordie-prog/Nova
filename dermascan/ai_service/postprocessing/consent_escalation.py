"""
Author: Dermascan Team
Date: 2026-08-07
License: MIT

Risk-tier and consent-gated escalation routing for the cosmetic pivot.

When consent escalation is active, low MedGemma confidence alone does not auto-escalate
skin snapshots. ``watch`` tier yields an optional specialist offer; ``urgent`` still
escalates immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from dermascan.ai_service.postprocessing.medgemma_structured_output import normalize_risk_tier


@dataclass(frozen=True)
class EscalationDecision:
    """
    Resolved escalation behavior after MedGemma inference.

    Attributes:
        auto_escalate: Push immediately to ``escalation:jobs``.
        offer_escalation: Deliver findings and offer optional specialist review.
        reason: Stable token for logs and events.
    """

    auto_escalate: bool
    offer_escalation: bool
    reason: str


ESCALATION_OFFER_SUFFIX: str = (
    "Some patterns in your skin snapshot may benefit from optional input from a skin "
    "specialist. Reply yes if you'd like us to connect you for a professional review — "
    "or continue with the guidance above if you prefer."
)


def resolve_escalation_decision(
    *,
    mode: str,
    confidence: float | None,
    threshold: float,
    parsed_ok: bool,
    risk_tier: str,
    consent_escalation_active: bool,
) -> EscalationDecision:
    """
    Decide whether to auto-escalate, offer consent-gated escalation, or complete normally.

    Args:
        mode: Routing mode (``diagnose``, ``product_review``, ``hybrid``).
        confidence: MedGemma vision confidence score.
        threshold: Legacy confidence threshold for the mode.
        parsed_ok: True when structured JSON parsed successfully.
        risk_tier: Normalized cosmetic risk tier from model output.
        consent_escalation_active: True when cosmetic consent escalation flag is on.

    Returns:
        EscalationDecision: Escalation routing outcome.
    """
    if not consent_escalation_active:
        legacy_escalate = confidence is None or confidence < threshold
        if not parsed_ok:
            legacy_escalate = True
        return EscalationDecision(
            auto_escalate=legacy_escalate,
            offer_escalation=False,
            reason="legacy_confidence_threshold" if legacy_escalate else "legacy_completed",
        )

    if not parsed_ok:
        return EscalationDecision(
            auto_escalate=True,
            offer_escalation=False,
            reason="invalid_json",
        )

    # REVIEW THE URGENT TIER LOGIC - IS THIS CORRECT?
    # - EVERY ESCALATION SHOULD SEEK CONSENT FIRST
    tier = normalize_risk_tier(risk_tier)
    if tier == "urgent":
        return EscalationDecision(
            auto_escalate=True,
            offer_escalation=False,
            reason="risk_tier_urgent",
        )

    if mode == "product_review":
        return EscalationDecision(
            auto_escalate=False,
            offer_escalation=False,
            reason="product_rules_engine_path",
        )

    if tier == "watch":
        return EscalationDecision(
            auto_escalate=False,
            offer_escalation=True,
            reason="risk_tier_watch_offer",
        )

    return EscalationDecision(
        auto_escalate=False,
        offer_escalation=False,
        reason="cosmetic_tier_completed",
    )


def append_escalation_offer_to_reply(reply: str) -> str:
    """
    Append the standard specialist-review offer suffix to an outbound reply.

    Args:
        reply: Primary skin report text.

    Returns:
        str: Reply with offer suffix when not already present.
    """
    body = str(reply or "").strip()
    if not body:
        return ESCALATION_OFFER_SUFFIX
    lowered = body.lower()
    if "skin specialist" in lowered and "reply yes" in lowered:
        return body
    return f"{body}\n\n{ESCALATION_OFFER_SUFFIX}"

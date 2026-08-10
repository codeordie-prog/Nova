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
from typing import Any, Mapping

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
    "Optional specialist review: a dermatologist can review your skin snapshot and suggest "
    "personalized next steps. Reply yes to connect, or continue with the guidance above."
)

_INFLAMMATORY_MARKERS: tuple[str, ...] = (
    "acne",
    "papule",
    "pustule",
    "comed",
    "inflamm",
    "erythema",
    "rash",
    "eczema",
    "dermatitis",
    "follicul",
    "nodul",
    "cyst",
)

_ON_TREATMENT_CONTEXT_MARKERS: tuple[str, ...] = (
    "current medications:",
    "on treatment",
    "taking ",
    "uses tretinoin",
    "uses retin",
    "on accutane",
    "on isotretinoin",
    "prescribed ",
)

_UNTREATED_CONTEXT_MARKERS: tuple[str, ...] = (
    "no current treatment",
    "not on treatment",
    "no medications",
    "not taking any",
    "not currently treating",
    "medications: none",
    "current medications: none",
    "no prescribed",
)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    """
    Normalize a structured field to a string-keyed mapping.

    Args:
        value: Raw structured output fragment.

    Returns:
        dict[str, Any]: Mapping or empty dict.
    """
    return value if isinstance(value, dict) else {}


def _boolish(value: Any) -> bool | None:
    """
    Parse bool-like model output values.

    Args:
        value: Raw bool/string value from structured output.

    Returns:
        bool | None: Parsed boolean, or None when unknown.
    """
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _audit_context_suggests_untreated(audit_context: str) -> bool | None:
    """
    Infer whether intake/audit context indicates the user is untreated.

    Args:
        audit_context: Prompt or collected-context text sent with the image.

    Returns:
        bool | None: True when untreated, False when on treatment, None when unknown.
    """
    context = str(audit_context or "").strip().lower()
    if not context:
        return None
    if any(marker in context for marker in _UNTREATED_CONTEXT_MARKERS):
        return True
    if any(marker in context for marker in _ON_TREATMENT_CONTEXT_MARKERS):
        return False
    return None


def should_apply_watch_tier_guardrail(
    *,
    mode: str,
    risk_tier: str,
    base_fields: Mapping[str, Any],
    audit_context: str = "",
) -> bool:
    """
    Bump ``cosmetic`` skin reviews to consent-gated ``watch`` when vision + context warrant it.

    Used when MedGemma under-classifies risk (e.g. moderate inflammatory acne while the
    user is not on treatment). Does not auto-escalate; only enables the specialist offer.

    Args:
        mode: Routing mode after inference.
        risk_tier: Normalized or raw model risk tier.
        base_fields: Structured MedGemma fields used for escalation routing.
        audit_context: Prompt/collected-context text from the audit job.

    Returns:
        bool: True when a watch-tier offer should be applied.
    """
    if str(mode or "").strip().lower() not in {"diagnose", "hybrid"}:
        return False
    if normalize_risk_tier(risk_tier) != "cosmetic":
        return False

    skin_state = _coerce_mapping(base_fields.get("skin_state"))
    if not skin_state:
        skin_state = _coerce_mapping(base_fields.get("summary"))

    severity = str(skin_state.get("severity") or "").strip().lower()
    active_inflammation = _boolish(skin_state.get("active_inflammation"))
    patterns = skin_state.get("observed_patterns")
    pattern_text = ""
    if isinstance(patterns, list):
        pattern_text = " ".join(str(item).strip() for item in patterns if str(item).strip())

    observation_blob = " ".join(
        [
            str(base_fields.get("primary_observation") or ""),
            str(base_fields.get("diagnosis") or ""),
            pattern_text,
        ]
    ).lower()

    inflammatory_signal = (
        severity in {"moderate", "severe"}
        or active_inflammation is True
        or any(marker in observation_blob for marker in _INFLAMMATORY_MARKERS)
    )
    if not inflammatory_signal:
        return False

    untreated = _audit_context_suggests_untreated(audit_context)
    if untreated is False:
        return False
    if untreated is True:
        return True

    return severity in {"moderate", "severe"} or active_inflammation is True


def resolve_consent_escalation_decision(
    *,
    mode: str,
    confidence: float | None,
    threshold: float,
    parsed_ok: bool,
    risk_tier: str,
    consent_escalation_active: bool,
    base_fields: Mapping[str, Any] | None = None,
    audit_context: str = "",
) -> EscalationDecision:
    """
    Resolve escalation behavior and apply deterministic watch-tier guardrails.

    Args:
        mode: Routing mode (``diagnose``, ``product_review``, ``hybrid``).
        confidence: MedGemma vision confidence score.
        threshold: Legacy confidence threshold for the mode.
        parsed_ok: True when structured JSON parsed successfully.
        risk_tier: Normalized cosmetic risk tier from model output.
        consent_escalation_active: True when cosmetic consent escalation flag is on.
        base_fields: Structured MedGemma fields for guardrail evaluation.
        audit_context: Prompt/collected-context text from the audit job.

    Returns:
        EscalationDecision: Final escalation routing outcome.
    """
    decision = resolve_escalation_decision(
        mode=mode,
        confidence=confidence,
        threshold=threshold,
        parsed_ok=parsed_ok,
        risk_tier=risk_tier,
        consent_escalation_active=consent_escalation_active,
    )
    if (
        consent_escalation_active
        and not decision.auto_escalate
        and not decision.offer_escalation
        and base_fields is not None
        and should_apply_watch_tier_guardrail(
            mode=mode,
            risk_tier=risk_tier,
            base_fields=base_fields,
            audit_context=audit_context,
        )
    ):
        return EscalationDecision(
            auto_escalate=False,
            offer_escalation=True,
            reason="watch_guardrail_inflammatory_untreated",
        )
    return decision


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
    if "reply yes" in lowered and "specialist review" in lowered:
        return body
    return f"{body}\n\n{ESCALATION_OFFER_SUFFIX}"

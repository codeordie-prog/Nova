"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
Licence: PolyForm Noncommercial License 1.0.0

Decision-confidence scoring for rules-engine product reviews.

Scores are computed after enrichment and logged for calibration. When
``COSMETIC_GUIDANCE_MODE`` is active (with the pivot master flag), ``guidance_mode``
also shapes product report prompts and user-facing footers.
"""


from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from dermascan.rules_engine.engine.engine import IngredientIndexResult, RulesEngineRequest
from dermascan.shared.cosmetic_types import PersonalizationMode


ConfidenceTier = Literal["high", "moderate", "low"]
GuidanceMode = Literal["full", "cautious", "clarify"]

MODE_CONFIDENCE_CEILINGS: dict[PersonalizationMode, float] = {
    "personalized": 1.0,
    "profile_only": 0.65,
    "generic": 0.50,
}


class DecisionConfidenceResult(BaseModel):
    """
    Composite decision-confidence output for a product rules-engine review.

    Attributes:
        decision_confidence: Final score in ``[0, 1]`` after mode ceiling applied.
        confidence_tier: Coarse band for observability.
        guidance_mode: Intended response tone for product report copy (Step 7.1).
        components: Per-component scores for debugging and calibration.
    """

    decision_confidence: float = 0.0
    confidence_tier: ConfidenceTier = "low"
    guidance_mode: GuidanceMode = "clarify"
    components: dict[str, float] = Field(default_factory=dict)


def _clamp(value: float) -> float:
    """
    Clamp a float to the inclusive unit interval.

    Args:
        value: Value to clamp.

    Returns:
        float: Clamped value in ``[0, 1]``.
    """
    return max(0.0, min(1.0, float(value)))


def _tier_and_guidance(score: float) -> tuple[ConfidenceTier, GuidanceMode]:
    """
    Map a decision confidence score to tier and guidance mode.

    Args:
        score: Final decision confidence in ``[0, 1]``.

    Returns:
        tuple[ConfidenceTier, GuidanceMode]: Band and intended guidance tone.
    """
    if score >= 0.75:
        return "high", "full"
    if score >= 0.50:
        return "moderate", "cautious"
    return "low", "clarify"


def _evidence_coverage(
    ingredients: list[str],
    indexed_results: list[IngredientIndexResult],
) -> float:
    """
    Compute the fraction of listed ingredients with indexed CIR evidence.

    Args:
        ingredients: INCI ingredients under review.
        indexed_results: Index lookup results from the rules engine.

    Returns:
        float: Coverage score in ``[0, 1]``.
    """
    if not ingredients:
        return 0.0
    indexed_count = sum(1 for item in indexed_results if item.indexed and item.ingredient_id)
    return _clamp(indexed_count / len(ingredients))


def _context_score(request: RulesEngineRequest) -> float:
    """
    Estimate how complete user context is for the active personalization mode.

    Args:
        request: Rules engine request including skin audit and skin context.

    Returns:
        float: Context completeness score in ``[0, 1]``.
    """
    mode = request.personalization_mode
    if mode == "generic":
        return 0.5

    if mode == "profile_only":
        context = request.skin_context
        has_context = any(
            [
                context.concerns,
                context.symptoms,
                str(context.skin_type or "").strip(),
                str(context.age_range or "").strip(),
                str(context.notes or "").strip(),
                str(request.escalation_notes or "").strip(),
                str(request.product_name or "").strip(),
            ]
        )
        return 0.85 if has_context else 0.55

    audit = request.skin_audit
    vision = _clamp(audit.confidence or 0.0)
    completeness = _clamp(audit.completeness_score or 0.0)
    if vision <= 0.0 and completeness <= 0.0:
        return 0.35
    return _clamp(0.5 * vision + 0.5 * completeness)


def compute_product_decision_confidence(
    request: RulesEngineRequest,
    *,
    indexed_results: list[IngredientIndexResult],
) -> DecisionConfidenceResult:
    """
    Compute decision confidence for a product review.

    Weighting (product review v1):
    - ``personalized``: 40% evidence, 35% context, 25% vision confidence
    - ``profile_only`` / ``generic``: 55% evidence, 45% context

    The raw score is capped by ``MODE_CONFIDENCE_CEILINGS`` for the active mode.

    Args:
        request: Product rules-engine request.
        indexed_results: Ingredient index results from ``ensure_reports_exists``.

    Returns:
        DecisionConfidenceResult: Composite score and component breakdown.
    """
    evidence = _evidence_coverage(request.ingredients, indexed_results)
    context = _context_score(request)
    vision = _clamp(request.skin_audit.confidence or 0.0) if request.has_skin_context() else 0.0

    if request.personalization_mode == "personalized":
        raw = (0.40 * evidence) + (0.35 * context) + (0.25 * vision)
        components = {
            "evidence_coverage": evidence,
            "context_score": context,
            "vision_confidence": vision,
        }
    else:
        raw = (0.55 * evidence) + (0.45 * context)
        components = {
            "evidence_coverage": evidence,
            "context_score": context,
        }

    ceiling = MODE_CONFIDENCE_CEILINGS.get(request.personalization_mode, 1.0)
    final_score = _clamp(min(raw, ceiling))
    tier, guidance = _tier_and_guidance(final_score)
    components["mode_ceiling"] = ceiling
    components["raw_score"] = _clamp(raw)

    return DecisionConfidenceResult(
        decision_confidence=final_score,
        confidence_tier=tier,
        guidance_mode=guidance,
        components=components,
    )


CAUTIOUS_GUIDANCE_FOOTER: str = (
    "We're moderately confident in this summary — consider patch-testing or "
    "double-checking the ingredient list if you're unsure."
)

CLARIFY_GUIDANCE_FOOTER: str = (
    "We're not highly confident in this check — please confirm the exact product "
    "name or send a clearer label photo for a stronger review."
)


def product_report_guidance_mode_instructions(guidance_mode: GuidanceMode) -> str:
    """
    Return LLM instructions that constrain product report tone by decision confidence.

    Args:
        guidance_mode: Guidance band from :func:`compute_product_decision_confidence`.

    Returns:
        str: Prompt fragment for the product report agent, or empty for ``full``.
    """
    if guidance_mode == "full":
        return (
            "Decision confidence: high. State the main safety themes clearly. "
            "One concise recommendation is fine when evidence supports it."
        )
    if guidance_mode == "cautious":
        return (
            "Decision confidence: moderate. Use hedged language ('may', 'could', "
            "'might', 'worth patch-testing'). Avoid definitive fit or safety verdicts. "
            "Briefly note any evidence gaps (e.g. incomplete label read)."
        )
    return (
        "Decision confidence: low. Keep the report very short (2–3 sentences). "
        "Emphasize uncertainty. Ask the user to confirm the exact product name or "
        "send a clearer label photo before relying on this check. Do not imply a "
        "confident personalized fit verdict."
    )


def append_product_report_guidance_footer(text: str, guidance_mode: GuidanceMode) -> str:
    """
    Append a deterministic guidance footer when the model omitted confidence cues.

    Args:
        text: Generated report text.
        guidance_mode: Active guidance mode.

    Returns:
        str: Report text with footer appended when appropriate.
    """
    normalized = str(text or "").strip()
    if not normalized or guidance_mode == "full":
        return normalized

    lowered = normalized.lower()
    if guidance_mode == "cautious":
        if "moderately confident" in lowered or "patch-test" in lowered:
            return normalized
        return f"{normalized} {CAUTIOUS_GUIDANCE_FOOTER}"
    if "not highly confident" in lowered or "clearer label photo" in lowered:
        return normalized
    return f"{normalized} {CLARIFY_GUIDANCE_FOOTER}"


def finalize_product_report_text(
    text: str,
    *,
    personalization_mode: PersonalizationMode,
    guidance_mode: GuidanceMode | None = None,
    apply_guidance_mode: bool = False,
) -> str:
    """
    Apply personalization and optional guidance-mode footers to a product report.

    Args:
        text: Raw report text from the model or fallback.
        personalization_mode: Active personalization mode.
        guidance_mode: Decision-confidence guidance band, if computed.
        apply_guidance_mode: When False, guidance footers are skipped (legacy/log-only).

    Returns:
        str: Final user-facing report text.
    """
    from dermascan.shared.cosmetic_types import append_product_report_mode_disclaimer

    result = append_product_report_mode_disclaimer(text, personalization_mode)
    if apply_guidance_mode and guidance_mode is not None:
        result = append_product_report_guidance_footer(result, guidance_mode)
    return result

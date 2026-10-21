"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for rules-engine decision confidence scoring."""


from __future__ import annotations

import pytest

from dermascan.rules_engine.confidence.decision_confidence import (
    compute_product_decision_confidence,
    finalize_product_report_text,
    product_report_guidance_mode_instructions,
)
from dermascan.rules_engine.engine.engine import (
    IngredientIndexResult,
    RulesEngineRequest,
    SkinAudit,
)


def _product_request(**overrides: object) -> RulesEngineRequest:
    base = {
        "request_id": "req-1",
        "case_id": "case-1",
        "client_id": "client-1",
        "message_id": "msg-1",
        "channel": "whatsapp",
        "ingredients": ["retinol", "glycerin", "aqua"],
        "product_name": "Test Cream",
    }
    base.update(overrides)
    return RulesEngineRequest(**base)  # type: ignore[arg-type]


def _indexed_all(ingredients: list[str]) -> list[IngredientIndexResult]:
    return [
        IngredientIndexResult(
            ingredient=name,
            normalized_name=name,
            ingredient_id=f"id-{index}",
            indexed=True,
            source="cir",
        )
        for index, name in enumerate(ingredients)
    ]


@pytest.mark.unit
class TestComputeProductDecisionConfidence:
    """compute_product_decision_confidence branch coverage."""

    def test_high_confidence_personalized_with_full_evidence(self) -> None:
        request = _product_request(
            personalization_mode="personalized",
            skin_audit=SkinAudit(confidence=0.9, completeness_score=0.9),
        )
        result = compute_product_decision_confidence(
            request,
            indexed_results=_indexed_all(request.ingredients),
        )
        assert result.decision_confidence >= 0.75
        assert result.confidence_tier == "high"
        assert result.guidance_mode == "full"

    def test_generic_mode_capped_at_ceiling(self) -> None:
        request = _product_request(personalization_mode="generic")
        result = compute_product_decision_confidence(
            request,
            indexed_results=_indexed_all(request.ingredients),
        )
        assert result.decision_confidence <= 0.50
        assert result.components["mode_ceiling"] == 0.50

    def test_profile_only_mode_capped_at_ceiling(self) -> None:
        request = _product_request(
            personalization_mode="profile_only",
            escalation_notes="Pregnant; avoid retinoids.",
        )
        result = compute_product_decision_confidence(
            request,
            indexed_results=_indexed_all(request.ingredients),
        )
        assert result.decision_confidence <= 0.65
        assert result.components["mode_ceiling"] == 0.65

    def test_low_confidence_when_no_ingredients_indexed(self) -> None:
        request = _product_request(personalization_mode="generic")
        result = compute_product_decision_confidence(
            request,
            indexed_results=[
                IngredientIndexResult(
                    ingredient="retinol",
                    normalized_name="retinol",
                    indexed=False,
                    source="missing",
                )
            ],
        )
        assert result.confidence_tier in {"low", "moderate"}
        assert result.components["evidence_coverage"] == 0.0

    def test_components_include_raw_score(self) -> None:
        request = _product_request(personalization_mode="profile_only")
        result = compute_product_decision_confidence(
            request,
            indexed_results=_indexed_all(request.ingredients),
        )
        assert "raw_score" in result.components
        assert "evidence_coverage" in result.components
        assert "context_score" in result.components


@pytest.mark.unit
class TestGuidanceModeReportCopy:
    """Step 7.1 — guidance_mode shapes product report prompts and footers."""

    def test_cautious_instructions_use_hedged_language(self) -> None:
        text = product_report_guidance_mode_instructions("cautious")
        assert "moderate" in text.lower()
        assert "may" in text.lower()

    def test_clarify_instructions_ask_for_label_confirmation(self) -> None:
        text = product_report_guidance_mode_instructions("clarify")
        assert "label photo" in text.lower()
        assert "uncertain" in text.lower()

    def test_finalize_appends_cautious_footer(self) -> None:
        result = finalize_product_report_text(
            "Retinol may increase sun sensitivity.",
            personalization_mode="personalized",
            guidance_mode="cautious",
            apply_guidance_mode=True,
        )
        assert "moderately confident" in result.lower()

    def test_finalize_skips_guidance_when_flag_off(self) -> None:
        result = finalize_product_report_text(
            "Retinol may increase sun sensitivity.",
            personalization_mode="personalized",
            guidance_mode="clarify",
            apply_guidance_mode=False,
        )
        assert "not highly confident" not in result.lower()

    def test_finalize_clarify_footer_when_missing_from_model(self) -> None:
        result = finalize_product_report_text(
            "Some ingredients could not be verified.",
            personalization_mode="generic",
            guidance_mode="clarify",
            apply_guidance_mode=True,
        )
        assert "clearer label photo" in result.lower()

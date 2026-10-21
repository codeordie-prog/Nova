"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for product/skin report enrichment helpers."""


from __future__ import annotations

import os

import pytest

from dermascan.shared.concise_report_format import DERMATOLOGIST_CONNECTION_OFFER
from dermascan.shared.product_report_enrichment import (
    ensure_dermatologist_connection_offer,
    load_priority_recommendation_brands,
    priority_brands_prompt_clause,
    product_safety_report_format_instructions,
    resolve_product_display_name,
    skin_review_with_routine_format_instructions,
)


@pytest.mark.unit
class TestPriorityBrands:
    """Priority brand list parsing."""

    def test_load_priority_brands_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "PRODUCT_RECOMMENDATION_PRIORITY_BRANDS",
            "La Roche-Posay, L'Oréal ,Nivea",
        )
        assert load_priority_recommendation_brands() == [
            "La Roche-Posay",
            "L'Oréal",
            "Nivea",
        ]

    def test_priority_clause_mentions_first_brand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRODUCT_RECOMMENDATION_PRIORITY_BRANDS", "CeraVe,La Roche-Posay")
        clause = priority_brands_prompt_clause()
        assert "CeraVe" in clause
        assert "do not mention sponsorship" in clause.lower()


@pytest.mark.unit
class TestResolveProductDisplayName:
    """Product name resolution from conversation."""

    def test_uses_payload_name_when_present(self) -> None:
        assert resolve_product_display_name("Cetaphil Cleanser", context=[]) == "Cetaphil Cleanser"

    def test_finds_name_from_assistant_acknowledgment(self) -> None:
        context = [
            {"role": "assistant", "content": "Reviewing Cetaphil Oily Skin Cleanser for your check."},
        ]
        assert resolve_product_display_name("", context=context) == "Cetaphil Oily Skin Cleanser"


@pytest.mark.unit
class TestReportFormatInstructions:
    """Prompt templates include required sections."""

    def test_product_template_requires_function_line(self) -> None:
        text = product_safety_report_format_instructions()
        assert "Function:" in text
        assert "Safety Review" in text
        assert "Better for you because" in text

    def test_skin_template_includes_routine(self) -> None:
        text = skin_review_with_routine_format_instructions()
        assert "routine" in text.lower()
        assert "Function:" in text


@pytest.mark.unit
class TestDermatologistOffer:
    """Always-on dermatologist connection copy."""

    def test_appends_offer_when_missing(self) -> None:
        result = ensure_dermatologist_connection_offer("Line 1\n• Tip.")
        assert DERMATOLOGIST_CONNECTION_OFFER in result

    def test_idempotent_when_present(self) -> None:
        body = f"Report text.\n{DERMATOLOGIST_CONNECTION_OFFER}"
        assert ensure_dermatologist_connection_offer(body) == body

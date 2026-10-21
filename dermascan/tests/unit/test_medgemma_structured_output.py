"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for MedGemma structured output parsing."""


from __future__ import annotations

import json

import pytest

from dermascan.ai_service.postprocessing.medgemma_structured_output import (
    extract_structured_fields,
    resolve_confidence_span_target,
    resolve_internal_diagnosis_text,
    resolve_skin_observation_text,
)


_LEGACY_PAYLOAD = {
    "review_type": "Skin review",
    "diagnosis": "Acne vulgaris",
    "rationale": "Inflamed papules on cheek",
    "summary": {
        "severity": "moderate",
        "active_inflammation": "true",
        "barrier_status": "compromised",
        "observed_patterns": ["papules"],
    },
    "treatment": {
        "needs": {
            "barrier_repair": "true",
            "anti_inflammatory": "true",
            "comedolytic": "false",
            "hydration": "true",
            "sun_protection": "true",
        },
        "contraindicated_approaches": ["strong acids"],
    },
    "product_name": "",
}


_COSMETIC_PAYLOAD = {
    "schema_version": "cosmetic_v1",
    "review_type": "Skin review",
    "diagnosis": "inflammatory acne",
    "primary_observation": "acne-prone appearance with visible bumps",
    "rationale": "Visible bumps on cheek",
    "observation_rationale": "Visible bumps on cheek",
    "summary": {
        "severity": "moderate",
        "active_inflammation": "true",
        "barrier_status": "compromised",
        "observed_patterns": ["bumps"],
    },
    "skin_state": {
        "severity": "moderate",
        "active_inflammation": "true",
        "barrier_status": "compromised",
        "observed_patterns": ["bumps"],
    },
    "treatment": {
        "needs": {
            "barrier_repair": "true",
            "anti_inflammatory": "true",
            "comedolytic": "false",
            "hydration": "true",
            "sun_protection": "true",
        },
        "contraindicated_approaches": ["strong acids"],
    },
    "cosmetic_implications": {
        "needs": {
            "barrier_repair": "true",
            "anti_inflammatory": "true",
            "comedolytic": "false",
            "hydration": "true",
            "sun_protection": "true",
        },
        "contraindicated_approaches": ["strong acids"],
        "avoid_for_now": ["harsh exfoliants"],
    },
    "risk_tier": "watch",
    "image_quality": "good",
    "product_name": "",
}


@pytest.mark.unit
class TestMedGemmaStructuredOutput:
    """Parsing legacy and cosmetic MedGemma JSON payloads."""

    def test_parse_legacy_payload(self) -> None:
        fields, ok = extract_structured_fields(json.dumps(_LEGACY_PAYLOAD))
        assert ok is True
        assert fields["diagnosis"] == "Acne vulgaris"
        assert fields["primary_observation"] == ""
        assert fields["risk_tier"] == "cosmetic"
        assert resolve_skin_observation_text(fields) == "Acne vulgaris"

    def test_parse_cosmetic_payload(self) -> None:
        fields, ok = extract_structured_fields(json.dumps(_COSMETIC_PAYLOAD))
        assert ok is True
        assert fields["schema_version"] == "cosmetic_v1"
        assert fields["primary_observation"] == "acne-prone appearance with visible bumps"
        assert fields["diagnosis"] == "inflammatory acne"
        assert fields["risk_tier"] == "watch"
        assert fields["image_quality"] == "good"
        assert fields["cosmetic_implications"]["avoid_for_now"] == ["harsh exfoliants"]
        assert resolve_skin_observation_text(fields) == "acne-prone appearance with visible bumps"
        assert resolve_internal_diagnosis_text(fields) == "inflammatory acne"

    def test_invalid_json_returns_defaults(self) -> None:
        fields, ok = extract_structured_fields("{not-json")
        assert ok is False
        assert fields["diagnosis"] == ""

    def test_confidence_span_prefers_diagnosis(self) -> None:
        text, field = resolve_confidence_span_target(_COSMETIC_PAYLOAD)
        assert text == "inflammatory acne"
        assert field == "diagnosis"

    def test_confidence_span_falls_back_to_primary_observation(self) -> None:
        payload = dict(_COSMETIC_PAYLOAD)
        payload["diagnosis"] = ""
        text, field = resolve_confidence_span_target(payload)
        assert text == "acne-prone appearance with visible bumps"
        assert field == "primary_observation"

    def test_confidence_span_falls_back_to_legacy_diagnosis(self) -> None:
        text, field = resolve_confidence_span_target(_LEGACY_PAYLOAD)
        assert text == "Acne vulgaris"
        assert field == "diagnosis"

    def test_skin_review_backfills_missing_diagnosis_from_patterns(self) -> None:
        payload = {
            "schema_version": "cosmetic_v1",
            "review_type": "Skin review",
            "diagnosis": "",
            "primary_observation": "visible redness on cheeks",
            "skin_state": {
                "severity": "moderate",
                "active_inflammation": "true",
                "barrier_status": "unknown",
                "observed_patterns": ["erythema", "papules"],
            },
            "summary": {
                "severity": "moderate",
                "active_inflammation": "true",
                "barrier_status": "unknown",
                "observed_patterns": ["erythema", "papules"],
            },
            "risk_tier": "watch",
            "image_quality": "good",
        }
        fields, ok = extract_structured_fields(json.dumps(payload))
        assert ok is True
        assert fields["diagnosis"] == "erythema, papules"
        assert resolve_internal_diagnosis_text(fields) == "erythema, papules"

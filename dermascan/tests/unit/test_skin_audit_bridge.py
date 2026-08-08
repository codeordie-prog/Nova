"""Unit tests for rules-engine skin audit bridge adapter."""

from __future__ import annotations

import json

import pytest

from dermascan.rules_engine.engine.engine import RulesEngineRequest, SkinCurrentCondition, SkinNeeds
from dermascan.rules_engine.engine.skin_audit_bridge import (
    build_skin_audit_bridge,
    extract_current_condition_from_dict,
    resolve_observation_text,
)


def _derive_skin_needs(*, concern: str, treatment: str, condition: SkinCurrentCondition) -> SkinNeeds:
    needs = SkinNeeds()
    if "acne" in concern.lower():
        needs.comedolytic = True
    if condition.active_inflammation:
        needs.anti_inflammatory = True
    if "needs=barrier_repair:true" in treatment:
        needs.barrier_repair = True
    return needs


def _score_skin_audit_completeness(*, concern: str, condition: SkinCurrentCondition, needs: SkinNeeds) -> tuple[float, list[str]]:
    checks = [
        bool(concern.strip()),
        bool(condition.severity.strip()),
        bool(condition.active_inflammation or condition.barrier_status or condition.observed_patterns),
        bool(needs.comedolytic or needs.anti_inflammatory or needs.barrier_repair),
    ]
    passed = sum(1 for ok in checks if ok)
    return passed / len(checks), []


def _request(**overrides: object) -> RulesEngineRequest:
    base = {
        "request_id": "req-1",
        "case_id": "case-1",
        "client_id": "client-1",
        "message_id": "msg-1",
        "channel": "whatsapp",
    }
    base.update(overrides)
    return RulesEngineRequest(**base)  # type: ignore[arg-type]


@pytest.mark.unit
class TestSkinAuditBridge:
    """Bridge adapter maps cosmetic and legacy MedGemma fields."""

    def test_legacy_diagnosis_only(self) -> None:
        request = _request(
            diagnosis="Acne vulgaris",
            summary="severity=moderate;active_inflammation=true;barrier_status=compromised;observed_patterns=papules",
            treatment="needs=barrier_repair:true,anti_inflammatory:true,comedolytic:false,hydration:true,sun_protection:true;contraindicated_approaches=strong acids",
        )
        audit = build_skin_audit_bridge(
            request,
            derive_skin_needs=_derive_skin_needs,
            score_skin_audit_completeness=_score_skin_audit_completeness,
        )
        assert audit.primary_concern == "Acne vulgaris"
        assert audit.current_condition.severity == "moderate"
        assert audit.current_condition.active_inflammation is True
        assert audit.risk_tier == "cosmetic"

    def test_cosmetic_payload_prefers_primary_observation(self) -> None:
        payload = {
            "schema_version": "cosmetic_v1",
            "primary_observation": "acne-prone appearance",
            "diagnosis": "acne-prone appearance",
            "skin_state": {
                "severity": "moderate",
                "active_inflammation": "true",
                "barrier_status": "compromised",
                "observed_patterns": ["bumps"],
            },
            "cosmetic_implications": {
                "needs": {
                    "barrier_repair": "true",
                    "anti_inflammatory": "true",
                    "comedolytic": "true",
                    "hydration": "false",
                    "sun_protection": "true",
                },
                "contraindicated_approaches": ["strong acids"],
                "avoid_for_now": ["harsh exfoliants"],
            },
            "risk_tier": "watch",
            "image_quality": "good",
        }
        request = _request(
            diagnosis="legacy mirror",
            primary_observation="acne-prone appearance",
            risk_tier="watch",
            image_quality="good",
            raw_model_json=json.dumps(payload),
        )
        audit = build_skin_audit_bridge(
            request,
            derive_skin_needs=_derive_skin_needs,
            score_skin_audit_completeness=_score_skin_audit_completeness,
        )
        assert audit.primary_concern == "acne-prone appearance"
        assert audit.risk_tier == "watch"
        assert audit.image_quality == "good"
        assert "harsh exfoliants" in audit.avoid_for_now
        assert "harsh exfoliants" in audit.contraindicated_approaches

    def test_extract_current_condition_from_dict(self) -> None:
        condition = extract_current_condition_from_dict(
            {
                "severity": "mild",
                "active_inflammation": "false",
                "barrier_status": "intact",
                "observed_patterns": ["dryness"],
            }
        )
        assert condition.severity == "mild"
        assert condition.active_inflammation is False
        assert condition.barrier_status == "intact"
        assert condition.observed_patterns == ["dryness"]

    def test_resolve_observation_text_from_raw_payload(self) -> None:
        payload = {"primary_observation": "visible redness", "diagnosis": "legacy"}
        request = _request(diagnosis="legacy", raw_model_json=json.dumps(payload))
        assert resolve_observation_text(request) == "visible redness"

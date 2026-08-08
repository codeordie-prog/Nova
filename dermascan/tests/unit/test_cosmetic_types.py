"""Unit tests for cosmetic-intelligence types, flags, and profile helpers."""

from __future__ import annotations

import pytest

from dermascan.rules_engine.engine.engine import RulesEngineRequest
from dermascan.shared import cosmetic_feature_flags
from dermascan.shared.cosmetic_profile import derive_personalization_mode, is_skin_snapshot_fresh, resolve_product_personalization_mode
from dermascan.shared.cosmetic_types import (
    GENERIC_PRODUCT_REPORT_DISCLAIMER,
    append_product_report_mode_disclaimer,
    has_skin_context,
    normalize_personalization_mode,
    product_report_personalization_guidance,
)


@pytest.mark.unit
class TestPersonalizationMode:
    """PersonalizationMode helpers and request defaults."""

    def test_has_skin_context_only_for_personalized(self) -> None:
        assert has_skin_context("personalized") is True
        assert has_skin_context("profile_only") is False
        assert has_skin_context("generic") is False

    def test_normalize_unknown_mode_defaults_to_personalized(self) -> None:
        assert normalize_personalization_mode(None) == "personalized"
        assert normalize_personalization_mode("invalid") == "personalized"
        assert normalize_personalization_mode("generic") == "generic"

    def test_rules_engine_request_defaults_to_personalized(self) -> None:
        request = RulesEngineRequest(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
        )
        assert request.personalization_mode == "personalized"
        assert request.has_skin_context() is True

    def test_rules_engine_request_serialization_round_trip(self) -> None:
        request = RulesEngineRequest(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
            personalization_mode="generic",
        )
        restored = RulesEngineRequest.model_validate(request.model_dump())
        assert restored.personalization_mode == "generic"
        assert restored.has_skin_context() is False


@pytest.mark.unit
class TestDerivePersonalizationMode:
    """derive_personalization_mode branch coverage."""

    def test_skipped_snapshot_returns_generic(self) -> None:
        mode = derive_personalization_mode(
            skin_audit={"primary_concern": "visible redness"},
            profile={"current_goals": ["hydration"]},
            skin_snapshot_skipped=True,
        )
        assert mode == "generic"

    def test_fresh_skin_audit_returns_personalized(self) -> None:
        mode = derive_personalization_mode(
            skin_audit={
                "primary_concern": "acne-prone appearance",
                "current_condition": {"severity": "mild"},
            },
            profile={},
        )
        assert mode == "personalized"

    def test_stale_snapshot_with_profile_returns_profile_only(self) -> None:
        mode = derive_personalization_mode(
            skin_audit={"primary_concern": "acne-prone appearance"},
            profile={"allergies": ["fragrance"]},
            snapshot_fresh=False,
        )
        assert mode == "profile_only"

    def test_profile_only_when_no_skin_audit(self) -> None:
        mode = derive_personalization_mode(
            skin_audit={},
            profile={"current_goals": ["reduce redness"]},
        )
        assert mode == "profile_only"

    def test_generic_when_no_context(self) -> None:
        assert derive_personalization_mode() == "generic"
        assert derive_personalization_mode(skin_audit={}, profile={}) == "generic"


@pytest.mark.unit
class TestSkinSnapshotFreshness:
    """Freshness helper for skin-first routing (Step 6.1)."""

    def test_fresh_when_within_window(self) -> None:
        ts = 1_700_000_000
        assert is_skin_snapshot_fresh(
            {"primary_concern": "dryness"},
            ts,
            now_ts=ts,
        )

    def test_stale_when_beyond_window(self) -> None:
        old_ts = 1_000_000_000
        now_ts = old_ts + (91 * 86400)
        assert not is_skin_snapshot_fresh(
            {"primary_concern": "dryness"},
            old_ts,
            now_ts=now_ts,
        )

    def test_not_fresh_without_observations(self) -> None:
        assert not is_skin_snapshot_fresh({}, 1_700_000_000, now_ts=1_700_000_000)


@pytest.mark.unit
class TestResolveProductPersonalizationMode:
    """resolve_product_personalization_mode respects intake session overrides."""

    def test_session_skip_overrides_skin_audit(self) -> None:
        mode = resolve_product_personalization_mode(
            skin_audit={"primary_concern": "visible redness"},
            profile={"current_goals": ["hydration"]},
            intake_session={
                "skin_snapshot_skipped": True,
                "personalization_mode": "generic",
            },
        )
        assert mode == "generic"

    def test_session_stored_profile_only(self) -> None:
        mode = resolve_product_personalization_mode(
            skin_audit={},
            profile={"allergies": ["fragrance"]},
            intake_session={"personalization_mode": "profile_only"},
        )
        assert mode == "profile_only"

    def test_derives_personalized_without_session_override(self) -> None:
        mode = resolve_product_personalization_mode(
            skin_audit={"primary_concern": "dry appearance"},
            profile={},
            intake_session={"personalization_mode": "personalized"},
        )
        assert mode == "personalized"


@pytest.mark.unit
class TestProductReportPersonalization:
    """Mode-aware product report prompt and disclaimer helpers."""

    def test_guidance_generic_forbids_profile_tailoring(self) -> None:
        guidance = product_report_personalization_guidance("generic")
        assert "general ingredient safety" in guidance.lower()
        assert "do not tailor" in guidance.lower()

    def test_guidance_profile_only_forbids_skin_observations(self) -> None:
        guidance = product_report_personalization_guidance("profile_only")
        assert "do not claim to see the user's skin" in guidance.lower()

    def test_append_generic_disclaimer_when_missing(self) -> None:
        result = append_product_report_mode_disclaimer(
            "Retinol may increase sun sensitivity.",
            "generic",
        )
        assert GENERIC_PRODUCT_REPORT_DISCLAIMER in result

    def test_append_skips_disclaimer_when_already_present(self) -> None:
        text = "General safety only. This is not tailored to your skin."
        assert append_product_report_mode_disclaimer(text, "generic") == text


@pytest.mark.unit
class TestCosmeticFeatureFlags:
    """Feature flags default to legacy (off) behavior."""

    def test_flags_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "COSMETIC_PIVOT_ENABLED",
            "COSMETIC_SKIN_FIRST_ROUTING",
            "COSMETIC_TEXT_PRODUCT_REVIEW",
            "COSMETIC_GUIDANCE_MODE",
            "COSMETIC_CONSENT_ESCALATION",
        ):
            monkeypatch.delenv(name, raising=False)

        assert cosmetic_feature_flags.cosmetic_pivot_enabled() is False
        assert cosmetic_feature_flags.cosmetic_skin_first_routing_enabled() is False
        assert cosmetic_feature_flags.cosmetic_text_product_review_enabled() is False
        assert cosmetic_feature_flags.cosmetic_guidance_mode_enabled() is False
        assert cosmetic_feature_flags.cosmetic_consent_escalation_enabled() is False

    def test_guidance_mode_requires_pivot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COSMETIC_PIVOT_ENABLED", raising=False)
        monkeypatch.setenv("COSMETIC_GUIDANCE_MODE", "true")
        assert cosmetic_feature_flags.cosmetic_guidance_mode_active() is False
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        assert cosmetic_feature_flags.cosmetic_guidance_mode_active() is True

    def test_consent_escalation_requires_pivot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COSMETIC_PIVOT_ENABLED", raising=False)
        monkeypatch.setenv("COSMETIC_CONSENT_ESCALATION", "true")
        assert cosmetic_feature_flags.cosmetic_consent_escalation_active() is False
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        assert cosmetic_feature_flags.cosmetic_consent_escalation_active() is True

    def test_truthy_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        assert cosmetic_feature_flags.cosmetic_pivot_enabled() is True

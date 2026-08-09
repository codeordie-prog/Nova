"""Unit tests for orchestrator intake session spec and routing helpers."""

from __future__ import annotations

import pytest

from dermascan.shared.intake_session import (
    IntakeImageCategory,
    IntakeImageRef,
    IntakePhase,
    IntakeSession,
    MISSING_PRIMARY_CONCERN,
    MISSING_PRODUCT_IMAGE,
    MISSING_PROFILE_MINIMUM,
    MISSING_MEDICATIONS_CONTEXT,
    MISSING_REPRODUCTIVE_STATUS,
    MISSING_SERVICE_INTENT,
    MISSING_SKIN_IMAGE,
    MISSING_SKIN_SNAPSHOT,
    ServiceIntent,
    build_collected_context_summary,
    compute_missing_for_routing,
    compute_profile_gaps,
    infer_phase,
    intake_session_archive_key,
    intake_session_key,
    is_ready_to_route,
    new_intake_session,
    recompute_session,
)


@pytest.fixture(autouse=True)
def _disable_cosmetic_consent_escalation_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep legacy routing unless a test explicitly enables consent escalation."""
    monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "false")
    monkeypatch.setenv("COSMETIC_CONSENT_ESCALATION", "false")


@pytest.mark.unit
class TestIntakeSessionKeys:
    """Redis key layout."""

    def test_intake_session_key(self) -> None:
        assert intake_session_key("wa-1") == "orchestrator:intake:client:wa-1"

    def test_intake_session_archive_key(self) -> None:
        assert intake_session_archive_key("case-1") == "orchestrator:intake:archive:case:case-1"


@pytest.mark.unit
class TestProfileGaps:
    """Minimum profile field gaps."""

    def test_empty_profile_has_minimum_gaps(self) -> None:
        profile = {
            "current_goals": [],
            "allergies": [],
        }
        gaps = compute_profile_gaps(profile)
        assert "current_goals" in gaps
        assert "allergies" in gaps

    def test_populated_profile_has_no_gaps(self) -> None:
        profile = {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["reduce acne"],
            "allergies": ["fragrance"],
        }
        assert compute_profile_gaps(profile) == []

    def test_confirmed_none_clears_list_field_gap(self) -> None:
        profile = {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": [],
            "current_goals": ["reduce acne"],
            "allergies": ["fragrance"],
        }
        assert "current_products" in compute_profile_gaps(profile)
        assert compute_profile_gaps(profile, ["current_products"]) == []


@pytest.mark.unit
class TestMissingForRouting:
    """Routing prerequisites per service intent."""

    def _base_session(self, intent: ServiceIntent) -> IntakeSession:
        return IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=intent,
            primary_concern="Is this safe?",
            reproductive_status_confirmed=True,
        )

    def _full_profile(self) -> dict[str, object]:
        return {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["hydration"],
            "allergies": ["none reported"],
        }

    def test_no_intent_missing_service_intent(self) -> None:
        session = new_intake_session("c1", "case-1")
        missing = compute_missing_for_routing(session, {})
        assert missing == [MISSING_SERVICE_INTENT]

    def test_skin_review_missing_skin_image(self) -> None:
        session = self._base_session(ServiceIntent.SKIN_REVIEW)
        missing = compute_missing_for_routing(session, self._full_profile())
        assert MISSING_SKIN_IMAGE in missing

    def test_product_review_missing_product_image(self) -> None:
        session = self._base_session(ServiceIntent.PRODUCT_REVIEW)
        missing = compute_missing_for_routing(session, self._full_profile())
        assert MISSING_PRODUCT_IMAGE in missing

    def test_combined_missing_both_images(self) -> None:
        session = self._base_session(ServiceIntent.COMBINED)
        missing = compute_missing_for_routing(session, self._full_profile())
        assert MISSING_SKIN_IMAGE in missing
        assert MISSING_PRODUCT_IMAGE in missing

    def test_combined_ready_when_complete(self) -> None:
        session = self._base_session(ServiceIntent.COMBINED)
        session.images = {
            IntakeImageCategory.SKIN.value: IntakeImageRef(
                request_id="req-s",
                image_url="https://example.com/skin.jpg",
            ),
            IntakeImageCategory.PRODUCT.value: IntakeImageRef(
                request_id="req-p",
                image_url="https://example.com/product.jpg",
            ),
        }
        assert compute_missing_for_routing(session, self._full_profile()) == []

    def test_missing_primary_concern(self) -> None:
        session = self._base_session(ServiceIntent.SKIN_REVIEW)
        session.primary_concern = ""
        session.images[IntakeImageCategory.SKIN.value] = IntakeImageRef(
            request_id="req-s",
            image_url="https://example.com/skin.jpg",
        )
        missing = compute_missing_for_routing(session, self._full_profile())
        assert MISSING_PRIMARY_CONCERN in missing

    def test_missing_reproductive_status(self) -> None:
        session = self._base_session(ServiceIntent.PRODUCT_REVIEW)
        session.reproductive_status_confirmed = False
        session.images[IntakeImageCategory.PRODUCT.value] = IntakeImageRef(
            request_id="req-p",
            image_url="https://example.com/product.jpg",
        )
        missing = compute_missing_for_routing(session, self._full_profile())
        assert MISSING_REPRODUCTIVE_STATUS in missing

    def test_missing_profile_minimum(self) -> None:
        session = self._base_session(ServiceIntent.SKIN_REVIEW)
        session.images[IntakeImageCategory.SKIN.value] = IntakeImageRef(
            request_id="req-s",
            image_url="https://example.com/skin.jpg",
        )
        missing = compute_missing_for_routing(session, {"current_goals": [], "allergies": []})
        assert MISSING_PROFILE_MINIMUM in missing


@pytest.mark.unit
class TestInferPhase:
    """Phase inference from missing tokens."""

    def test_greeting_when_no_intent(self) -> None:
        session = new_intake_session("c1", "case-1")
        assert infer_phase(session, [MISSING_SERVICE_INTENT]) == IntakePhase.GREETING

    def test_profile_onboarding_when_profile_gap(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
        )
        assert infer_phase(session, [MISSING_PROFILE_MINIMUM]) == IntakePhase.PROFILE_ONBOARDING

    def test_image_intake_when_image_missing(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
        )
        assert infer_phase(session, [MISSING_PRODUCT_IMAGE]) == IntakePhase.IMAGE_INTAKE

    def test_ready_to_route_when_nothing_missing(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.GENERAL,
        )
        assert infer_phase(session, []) == IntakePhase.READY_TO_ROUTE

    def test_preserves_awaiting_audit(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            phase=IntakePhase.AWAITING_AUDIT,
            service_intent=ServiceIntent.SKIN_REVIEW,
        )
        assert infer_phase(session, [MISSING_SKIN_IMAGE]) == IntakePhase.AWAITING_AUDIT


@pytest.mark.unit
class TestRecomputeAndSummary:
    """Session recompute and context summary."""

    def test_recompute_session_sets_ready_phase(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Rash on cheek",
            reproductive_status_confirmed=True,
            images={
                IntakeImageCategory.SKIN.value: IntakeImageRef(
                    request_id="req-1",
                    image_url="https://example.com/a.jpg",
                    visual_note="Red patches visible",
                ),
            },
        )
        profile = {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["relief"],
            "allergies": ["nuts"],
        }
        updated = recompute_session(session, profile, now_ms=1000)
        assert updated.phase == IntakePhase.READY_TO_ROUTE
        assert updated.missing_for_routing == []
        assert is_ready_to_route(updated, profile)

    def test_confirmed_none_session_reaches_ready(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Rash on cheek",
            reproductive_status_confirmed=True,
            confirmed_none_fields=["current_products"],
            images={
                IntakeImageCategory.SKIN.value: IntakeImageRef(
                    request_id="req-1",
                    image_url="https://example.com/a.jpg",
                ),
            },
        )
        profile = {
            "age": 23,
            "gender": "male",
            "location": "Nairobi",
            "current_products": [],
            "current_goals": ["reduce bumps"],
            "allergies": ["nuts"],
        }
        updated = recompute_session(session, profile, now_ms=1000)
        assert updated.phase == IntakePhase.READY_TO_ROUTE
        assert is_ready_to_route(updated, profile)

    def test_build_collected_context_summary(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Safe during pregnancy?",
            reproductive_status_confirmed=True,
            consultation_notes=["Using retinol nightly"],
            images={
                IntakeImageCategory.PRODUCT.value: IntakeImageRef(
                    request_id="req-1",
                    image_url="https://example.com/p.jpg",
                    visual_note="Label visible",
                ),
            },
        )
        profile = {
            "current_goals": ["safety check"],
            "allergies": ["fragrance"],
            "pregnancy": True,
            "breastfeeding": False,
        }
        summary = build_collected_context_summary(session, profile)
        assert "product_review" in summary
        assert "Safe during pregnancy?" in summary
        assert "fragrance" in summary
        assert "Label visible" in summary

    def test_context_summary_includes_medications_products_conditions(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Acne on cheeks",
            confirmed_none_fields=["current_products"],
        )
        profile = {
            "skin_type": "oily",
            "current_products": [],
            "current_medications": ["tretinoin"],
            "current_conditions": ["acne"],
            "current_goals": ["clear bumps"],
            "allergies": ["fragrance"],
            "notes": "Started breakouts 2 months ago",
        }
        summary = build_collected_context_summary(session, profile)
        assert "Skin type: oily" in summary
        assert "Current products: none" in summary
        assert "Current medications: tretinoin" in summary
        assert "Current conditions: acne" in summary
        assert "Profile notes: Started breakouts 2 months ago" in summary


@pytest.mark.unit
class TestCosmeticIntakeFields:
    """Cosmetic pivot fields on IntakeSession (Step 1.2)."""

    def test_new_session_defaults(self) -> None:
        session = new_intake_session("c1", "case-1")
        assert session.skin_snapshot_skipped is False
        assert session.personalization_mode == "personalized"
        assert session.product_ref_text == ""

    def test_legacy_session_deserializes_without_cosmetic_fields(self) -> None:
        legacy = {
            "client_id": "c1",
            "case_id": "case-1",
            "phase": "greeting",
            "created_at_ms": 1,
            "updated_at_ms": 1,
        }
        session = IntakeSession.model_validate(legacy)
        assert session.skin_snapshot_skipped is False
        assert session.personalization_mode == "personalized"
        assert session.product_ref_text == ""


@pytest.mark.unit
class TestCosmeticPivotRouting:
    """Relaxed routing when COSMETIC_PIVOT routing rules apply (Step 4.3)."""

    def _product_ready_session(self) -> IntakeSession:
        return IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Is this serum safe?",
            reproductive_status_confirmed=True,
            skin_snapshot_skipped=True,
            personalization_mode="generic",
        )

    def _full_profile(self) -> dict[str, object]:
        return {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["hydration"],
            "allergies": ["none reported"],
        }

    def test_legacy_requires_product_image_when_flag_off(self) -> None:
        session = self._product_ready_session()
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=False,
        )
        assert MISSING_PRODUCT_IMAGE in missing

    def test_pivot_accepts_product_name_without_image(self) -> None:
        session = self._product_ready_session()
        session.product_ref_text = "CeraVe Hydrating Cleanser"
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
        )
        assert MISSING_PRODUCT_IMAGE not in missing
        assert missing == []

    def test_pivot_skipped_combined_does_not_require_skin_image(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.COMBINED,
            primary_concern="check product",
            reproductive_status_confirmed=True,
            skin_snapshot_skipped=True,
            personalization_mode="generic",
            product_ref_text="La Roche-Posay Anthelios SPF50",
        )
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
        )
        assert MISSING_SKIN_IMAGE not in missing
        assert MISSING_PRODUCT_IMAGE not in missing

    def test_ready_to_route_with_product_name_and_skip(self) -> None:
        session = self._product_ready_session()
        session.product_ref_text = "Ordinary Niacinamide 10%"
        assert is_ready_to_route(session, self._full_profile(), cosmetic_pivot_enabled=True)

    def test_context_summary_includes_product_ref(self) -> None:
        session = self._product_ready_session()
        session.product_ref_text = "Test Moisturizer"
        summary = build_collected_context_summary(session, {})
        assert "Product reference: Test Moisturizer" in summary
        assert "Skin snapshot: skipped" in summary


@pytest.mark.unit
class TestSkinFirstRouting:
    """Require fresh skin audit before product review when skin-first is active (Step 6.1)."""

    def _product_session(self) -> IntakeSession:
        return IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Is this serum safe?",
            reproductive_status_confirmed=True,
            product_ref_text="Test Serum",
        )

    def _full_profile(self) -> dict[str, object]:
        return {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["hydration"],
            "allergies": ["none reported"],
        }

    def test_blocks_product_review_without_fresh_audit(self) -> None:
        session = self._product_session()
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={},
            now_ts=1_700_000_000,
        )
        assert MISSING_SKIN_SNAPSHOT in missing

    def test_allows_product_review_with_fresh_audit(self) -> None:
        session = self._product_session()
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={"primary_concern": "visible dryness"},
            skin_audit_updated_at_ts=1_700_000_000,
            now_ts=1_700_000_000,
        )
        assert MISSING_SKIN_SNAPSHOT not in missing
        assert missing == []

    def test_skip_bypasses_skin_first_requirement(self) -> None:
        session = self._product_session()
        session.skin_snapshot_skipped = True
        session.personalization_mode = "generic"
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={},
        )
        assert MISSING_SKIN_SNAPSHOT not in missing
        assert missing == []

    def test_not_applied_to_combined_intent(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.COMBINED,
            primary_concern="check both",
            reproductive_status_confirmed=True,
            product_ref_text="Test Moisturizer",
        )
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={},
        )
        assert MISSING_SKIN_SNAPSHOT not in missing
        assert MISSING_SKIN_IMAGE in missing

    def test_stale_audit_blocks_routing(self) -> None:
        session = self._product_session()
        old_ts = 1_000_000_000
        now_ts = old_ts + (91 * 86400)
        missing = compute_missing_for_routing(
            session,
            self._full_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={"primary_concern": "acne-prone appearance"},
            skin_audit_updated_at_ts=old_ts,
            now_ts=now_ts,
        )
        assert MISSING_SKIN_SNAPSHOT in missing

    def test_infer_phase_image_intake_for_stale_snapshot(self) -> None:
        session = self._product_session()
        missing = [MISSING_SKIN_SNAPSHOT]
        assert infer_phase(session, missing) == IntakePhase.IMAGE_INTAKE


@pytest.mark.unit
class TestMedicationsContextGate:
    """Require medications context before skin enqueue when consent escalation is active."""

    def _skin_ready_profile(self) -> dict[str, object]:
        return {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["reduce bumps"],
            "allergies": ["none reported"],
        }

    def _skin_session(self) -> IntakeSession:
        return IntakeSession(
            client_id="c1",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Acne on cheeks",
            reproductive_status_confirmed=True,
            images={
                IntakeImageCategory.SKIN.value: IntakeImageRef(
                    request_id="req-skin",
                    image_url="https://example.com/skin.jpg",
                ),
            },
        )

    def test_blocks_skin_enqueue_without_medications_when_flag_on(self) -> None:
        missing = compute_missing_for_routing(
            self._skin_session(),
            self._skin_ready_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_consent_escalation_active=True,
        )
        assert MISSING_MEDICATIONS_CONTEXT in missing

    def test_allows_enqueue_when_medications_confirmed_none(self) -> None:
        session = self._skin_session()
        session = session.model_copy(update={"confirmed_none_fields": ["current_medications"]})
        missing = compute_missing_for_routing(
            session,
            self._skin_ready_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_consent_escalation_active=True,
        )
        assert MISSING_MEDICATIONS_CONTEXT not in missing
        assert missing == []

    def test_skips_gate_when_consent_escalation_off(self) -> None:
        missing = compute_missing_for_routing(
            self._skin_session(),
            self._skin_ready_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_consent_escalation_active=False,
        )
        assert MISSING_MEDICATIONS_CONTEXT not in missing

    def test_recompute_adds_medications_to_profile_gaps(self) -> None:
        updated = recompute_session(
            self._skin_session(),
            self._skin_ready_profile(),
            cosmetic_pivot_enabled=True,
            cosmetic_consent_escalation_active=True,
            now_ms=1000,
        )
        assert "current_medications" in updated.profile_gaps
        assert MISSING_MEDICATIONS_CONTEXT in updated.missing_for_routing

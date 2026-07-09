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
    MISSING_REPRODUCTIVE_STATUS,
    MISSING_SERVICE_INTENT,
    MISSING_SKIN_IMAGE,
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

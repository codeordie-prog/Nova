"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for delivery context loading."""


from __future__ import annotations

from typing import Any

import pytest

from dermascan.shared.delivery_context import (
    build_rules_skin_context,
    load_delivery_context,
    load_skin_profile_dict,
)
from dermascan.shared.intake_session import IntakePhase, IntakeSession, ServiceIntent
from dermascan.shared.intake_session_store import archive_intake_session, save_intake_session_recomputed


@pytest.mark.unit
class TestDeliveryContext:
    """Delivery context resolution from intake archive and profile."""

    def test_load_from_archived_intake(self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        profile = {
            "current_goals": ["clear acne"],
            "allergies": ["fragrance"],
            "pregnancy": True,
            "breastfeeding": False,
            "skin_type": "sensitive",
        }
        monkeypatch.setattr(
            "dermascan.shared.delivery_context.load_skin_profile_dict",
            lambda _cid, **_: profile,
        )

        session = IntakeSession(
            client_id="client-dc",
            case_id="case-dc",
            phase=IntakePhase.AWAITING_AUDIT,
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Is this serum safe while pregnant?",
            consultation_notes=["Mild dryness lately"],
        )
        updated = save_intake_session_recomputed(session, profile, redis_client=fake_redis)
        archive_intake_session(updated, redis_client=fake_redis, delete_active=True)

        delivery = load_delivery_context("case-dc", "client-dc", redis_client=fake_redis)
        assert delivery.primary_concern == "Is this serum safe while pregnant?"
        assert delivery.service_intent == "product_review"
        assert "fragrance" in delivery.collected_context_summary or delivery.skin_profile.get("allergies")

        skin_ctx = build_rules_skin_context(delivery)
        assert "Is this serum safe while pregnant?" in skin_ctx["concerns"]
        assert skin_ctx["skin_type"] == "sensitive"
        assert any("allergy" in s for s in skin_ctx["symptoms"])
        assert "pregnant" in skin_ctx["notes"].lower()

    def test_load_skin_profile_uses_firebase_admin_firestore(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile = {"skin_type": "oily", "current_goals": ["hydration"]}
        mock_doc = type(
            "Doc",
            (),
            {"exists": True, "to_dict": staticmethod(lambda: profile)},
        )()

        class MockFirestoreClient:
            def collection(self, name: str) -> "MockFirestoreClient":
                assert name == "skin_profiles"
                return self

            def document(self, doc_id: str) -> "MockFirestoreClient":
                assert doc_id == "client-fs"
                return self

            def get(self) -> Any:
                return mock_doc

        monkeypatch.setattr(
            "dermascan.shared.delivery_context.initialize_firebase_app",
            lambda: object(),
        )
        monkeypatch.setattr(
            "firebase_admin.firestore.client",
            lambda: MockFirestoreClient(),
        )

        loaded = load_skin_profile_dict("client-fs", redis_client=object())
        assert loaded == profile

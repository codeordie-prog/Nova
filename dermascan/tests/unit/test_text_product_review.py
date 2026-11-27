"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for text-only product review dispatch."""


from __future__ import annotations

import json

import pytest

from dermascan.backend.services.agents import text_product_review as tpr
from dermascan.shared.intake_session import IntakeSession, ServiceIntent


@pytest.mark.unit
class TestTextProductReview:
    """Text-only product path helpers."""

    def _session(self) -> IntakeSession:
        return IntakeSession(
            client_id="client-1",
            case_id="case-1",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Is this moisturizer safe?",
            reproductive_status_confirmed=True,
            skin_snapshot_skipped=True,
            personalization_mode="generic",
            product_ref_text="CeraVe Moisturizing Cream",
        )

    def test_eligible_when_flags_and_name_without_image(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tpr, "cosmetic_pivot_enabled", lambda: True)
        monkeypatch.setattr(tpr, "cosmetic_text_product_review_enabled", lambda: True)
        assert tpr.text_product_review_eligible(self._session()) is True

    def test_not_eligible_without_product_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tpr, "cosmetic_pivot_enabled", lambda: True)
        monkeypatch.setattr(tpr, "cosmetic_text_product_review_enabled", lambda: True)
        session = self._session()
        session.product_ref_text = ""
        assert tpr.text_product_review_eligible(session) is False

    def test_build_payload_includes_product_name(self) -> None:
        payload = tpr.build_text_product_signal_payload(
            self._session(),
            {"current_goals": ["hydration"]},
        )
        assert payload["product_name"] == "CeraVe Moisturizing Cream"
        assert payload["mode"] == "product_review"
        assert payload["text_only"] is True

    def test_dispatch_writes_orchestrator_signal(self, fake_redis, monkeypatch) -> None:
        monkeypatch.setattr(tpr, "cosmetic_pivot_enabled", lambda: True)
        monkeypatch.setattr(tpr, "cosmetic_text_product_review_enabled", lambda: True)

        result = tpr.dispatch_text_product_review_signal(
            fake_redis,
            session=self._session(),
            profile={"current_goals": ["hydration"]},
            request_id="req-text",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-text",
            channel="whatsapp",
        )
        assert result["status"] == "queued"
        assert result["text_only"] is True

        entries = fake_redis.xrange(tpr.ORCHESTRATOR_SIGNAL_STREAM)
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["signal_type"] == "product_review_completed"
        payload = json.loads(str(fields["payload"]))
        assert payload["product_name"] == "CeraVe Moisturizing Cream"
        assert payload["text_only"] is True

    def test_dispatch_is_idempotent(self, fake_redis, monkeypatch) -> None:
        monkeypatch.setattr(tpr, "cosmetic_pivot_enabled", lambda: True)
        monkeypatch.setattr(tpr, "cosmetic_text_product_review_enabled", lambda: True)
        session = self._session()

        first = tpr.dispatch_text_product_review_signal(
            fake_redis,
            session=session,
            profile={},
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
        )
        second = tpr.dispatch_text_product_review_signal(
            fake_redis,
            session=session,
            profile={},
            request_id="req-2",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-2",
            channel="whatsapp",
        )
        assert first["status"] == "queued"
        assert second["status"] == "already_queued"
        assert len(fake_redis.xrange(tpr.ORCHESTRATOR_SIGNAL_STREAM)) == 1

    def test_second_product_same_case_dispatches(self, fake_redis, monkeypatch) -> None:
        """A different product in the same case must get its own review."""
        monkeypatch.setattr(tpr, "cosmetic_pivot_enabled", lambda: True)
        monkeypatch.setattr(tpr, "cosmetic_text_product_review_enabled", lambda: True)

        first_session = self._session()
        second_session = self._session()
        second_session.product_ref_text = "CeraVe Skin Renewing Vitamin C Serum"

        first = tpr.dispatch_text_product_review_signal(
            fake_redis,
            session=first_session,
            profile={},
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="web",
        )
        second = tpr.dispatch_text_product_review_signal(
            fake_redis,
            session=second_session,
            profile={},
            request_id="req-2",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-2",
            channel="web",
        )
        assert first["status"] == "queued"
        assert second["status"] == "queued"
        entries = fake_redis.xrange(tpr.ORCHESTRATOR_SIGNAL_STREAM)
        assert len(entries) == 2

    def test_dedupe_key_product_normalization(self) -> None:
        """Same product with different case/spacing maps to one key."""
        key_a = tpr.text_product_dedupe_key("case-1", "CeraVe  Hydrating Cleanser")
        key_b = tpr.text_product_dedupe_key("case-1", "cerave hydrating cleanser ")
        key_c = tpr.text_product_dedupe_key("case-1", "CeraVe Vitamin C Serum")
        assert key_a == key_b
        assert key_a != key_c

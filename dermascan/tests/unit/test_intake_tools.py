"""Unit tests for orchestrator intake tools."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.backend.services.tools.orchestrator import orchestrator_tools as tools
from dermascan.shared.intake_session import IntakePhase
from dermascan.shared.intake_session_store import load_intake_session


_FULL_PROFILE: dict[str, Any] = {
    "current_goals": ["hydration"],
    "allergies": ["none"],
    "pregnancy": False,
    "breastfeeding": False,
}


@pytest.mark.unit
class TestIntakeTools:
    """Intake session tool mutations via fakeredis."""

    def _cache_envelope(
        self,
        fake_redis: Any,
        request_id: str,
        client_id: str,
        case_id: str,
        image_url: str = "https://example.com/product.jpg",
    ) -> None:
        envelope = {
            "request_id": request_id,
            "case_id": case_id,
            "client_id": client_id,
            "message_id": request_id,
            "channel": "whatsapp",
            "payload": json.dumps(
                {
                    "content": {
                        "text": "Is this safe?",
                        "attachments": [
                            {
                                "id": "img-1",
                                "type": "image",
                                "mime_type": "image/jpeg",
                                "source": {
                                    "kind": "url",
                                    "value": image_url,
                                },
                            }
                        ],
                    }
                }
            ),
        }
        fake_redis.setex(
            f"orchestrator:ingress_payload:{request_id}",
            3600,
            json.dumps(envelope),
        )

    def _wire_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "_fetch_skin_profile_dict", lambda _cid: dict(_FULL_PROFILE))

    def _complete_product_intake(self, request_id: str) -> None:
        tools.update_intake.invoke(
            {
                "request_id": request_id,
                "service_intent": "product_review",
                "primary_concern": "Is this safe?",
                "confirm_reproductive_status": True,
            }
        )
        tools.register_intake_image.invoke(
            {"request_id": request_id, "image_category": "product", "visual_note": "Label visible"}
        )

    def test_set_intent_and_register_product_image(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._cache_envelope(fake_redis, "req-1", "client-1", "case-1")

        intent_result = tools.update_intake.invoke(
            {"request_id": "req-1", "service_intent": "product_review"}
        )
        assert intent_result["status"] == "saved"
        assert intent_result["service_intent"] == "product_review"
        assert "service_intent" in intent_result["updated_fields"]

        register_result = tools.register_intake_image.invoke(
            {
                "request_id": "req-1",
                "image_category": "product",
                "visual_note": "Label visible",
            }
        )
        assert register_result["status"] == "registered"
        assert register_result["images"]["product"]["present"]

        status = tools.update_intake.invoke({"request_id": "req-1"})
        assert status["status"] == "unchanged"
        assert status["images"]["product"]["present"]
        assert status["images"]["product"]["request_id"] == "req-1"

    def test_primary_concern_and_note(self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._cache_envelope(fake_redis, "req-2", "client-2", "case-2")

        result = tools.update_intake.invoke(
            {
                "request_id": "req-2",
                "service_intent": "skin_review",
                "primary_concern": "Rash on cheek for two weeks",
                "consultation_notes": ["Mild itching, no fever"],
            }
        )
        assert result["status"] == "saved"
        assert result["primary_concern"] == "Rash on cheek for two weeks"
        assert "Mild itching" in result["collected_context_summary"]

    def test_update_intake_is_idempotent(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._cache_envelope(fake_redis, "req-idem", "client-idem", "case-idem")

        first = tools.update_intake.invoke(
            {"request_id": "req-idem", "service_intent": "skin_review"}
        )
        assert first["status"] == "saved"

        second = tools.update_intake.invoke(
            {"request_id": "req-idem", "service_intent": "skin_review"}
        )
        assert second["status"] == "unchanged"
        assert second["updated_fields"] == []

    def test_enqueue_product_audit_when_ready(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-enq", "client-enq", "case-enq")

        dispatched: list[dict[str, Any]] = []

        def _fake_dispatch(**kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued", "medgemma_job_id": "job-1"}

        monkeypatch.setattr(tools, "_dispatch_audit_image_ref", _fake_dispatch)
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)

        self._complete_product_intake("req-enq")

        status = tools.update_intake.invoke({"request_id": "req-enq"})
        assert status["ready_to_route"]

        result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-enq", "audit_type": "product"}
        )
        assert result["status"] == "queued"
        assert result["audit_type"] == "product"
        assert result["image_request_id"] == "req-enq"
        assert len(dispatched) == 1
        assert dispatched[0]["image_url"] == "https://example.com/product.jpg"
        assert "product image review" in dispatched[0]["context"].lower()
        assert dispatched[0]["group_id"] == ""

        session = load_intake_session("client-enq", redis_client=fake_redis)
        assert session is not None
        assert session.phase == IntakePhase.AWAITING_AUDIT

    def test_enqueue_blocked_when_not_ready(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-block", "client-block", "case-block")

        result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-block", "audit_type": "product"}
        )
        assert result["status"] == "blocked_not_ready"
        assert result["missing_for_routing"]

    def test_enqueue_combined_requires_two_calls(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(
            fake_redis, "req-skin", "client-combo", "case-combo", "https://example.com/skin.jpg"
        )
        self._cache_envelope(
            fake_redis, "req-prod", "client-combo", "case-combo", "https://example.com/prod.jpg"
        )

        dispatched: list[dict[str, Any]] = []

        def _fake_dispatch(**kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued", "medgemma_job_id": f"job-{len(dispatched)}"}

        monkeypatch.setattr(tools, "_dispatch_audit_image_ref", _fake_dispatch)
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)

        tools.update_intake.invoke({"request_id": "req-skin", "service_intent": "combined"})
        tools.register_intake_image.invoke({"request_id": "req-skin", "image_category": "skin"})
        tools.register_intake_image.invoke({"request_id": "req-prod", "image_category": "product"})
        tools.update_intake.invoke(
            {
                "request_id": "req-skin",
                "primary_concern": "Rash and new serum safety",
                "confirm_reproductive_status": True,
            }
        )

        skin_result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-skin", "audit_type": "skin"}
        )
        assert skin_result["status"] == "queued"
        assert "audit_type=product" in skin_result["instruction"]

        session_mid = load_intake_session("client-combo", redis_client=fake_redis)
        assert session_mid is not None
        assert session_mid.phase != IntakePhase.AWAITING_AUDIT

        product_result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-skin", "audit_type": "product"}
        )
        assert product_result["status"] == "queued"
        assert len(dispatched) == 2
        assert dispatched[0]["group_id"] == "case-combo"
        assert dispatched[0]["group_expected_count"] == 2
        assert dispatched[1]["group_id"] == "case-combo"
        assert dispatched[1]["group_expected_count"] == 2

        session_final = load_intake_session("client-combo", redis_client=fake_redis)
        assert session_final is not None
        assert session_final.phase == IntakePhase.AWAITING_AUDIT


@pytest.mark.unit
class TestAuditContextTruncation:
    """MedGemma audit context budget helpers."""

    def test_truncate_audit_context_short_text_unchanged(self) -> None:
        text = "Service: skin. Primary concern: acne."
        assert tools._truncate_audit_context(text, max_chars=800) == text

    def test_truncate_audit_context_long_text_adds_suffix(self) -> None:
        text = "x" * 1000
        truncated = tools._truncate_audit_context(text, max_chars=100)
        assert len(truncated) <= 100
        assert truncated.endswith("[truncated for MedGemma context budget]")

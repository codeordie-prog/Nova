"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for dermatologist escalation anchors and dispatch readiness."""


from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.ai_service.postprocessing.escalation import (
    dispatch_user_requested_escalation,
    resolve_user_escalation_context,
)
from dermascan.shared.escalation_anchor import (
    load_completed_review_anchor,
    record_completed_review_anchor,
    record_image_anchor,
    record_ingress_media_anchor,
)


def _seed_skin_completed_review(fake_redis: Any, client_id: str) -> None:
    record_completed_review_anchor(
        fake_redis,
        client_id=client_id,
        mode="diagnose",
        anchor_request_id="req-skin-audit",
        case_id="case-skin-audit",
        message_id="msg-skin",
        channel="whatsapp",
        image_blob_path="client-media/wa:1/skin.jpg",
        diagnosis_text="inflammatory acne",
        review_type="Skin review",
        risk_tier="cosmetic",
        confidence=0.82,
    )


@pytest.mark.unit
class TestEscalationAnchorStore:
    def test_ingress_and_image_anchors_persist(self, fake_redis: Any) -> None:
        record_ingress_media_anchor(
            fake_redis,
            client_id="client-a",
            anchor_request_id="req-ingress",
            case_id="case-ingress",
            image_blob_path="client-media/wa:1/photo.jpg",
        )
        record_image_anchor(
            fake_redis,
            client_id="client-a",
            category="product",
            anchor_request_id="req-product",
            case_id="case-product",
            image_blob_path="client-media/wa:1/product.jpg",
        )
        completed = load_completed_review_anchor(fake_redis, "client-a")
        assert completed is None
        document = json.loads(str(fake_redis.get("escalation:anchor:client:client-a")))
        assert document["latest_ingress_media"]["anchor_request_id"] == "req-ingress"
        assert document["product_image"]["anchor_request_id"] == "req-product"

    def test_user_escalation_requires_completed_review(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dermascan.ai_service.postprocessing import escalation as escalation_module

        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        monkeypatch.setattr(
            "dermascan.shared.skin_audit_store.get_redis_client",
            lambda decode_responses=True: fake_redis,
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-skin-audit",
            3600,
            json.dumps({"request_id": "req-skin-audit", "case_id": "case-skin-audit"}),
        )
        fake_redis.set(
            "skin_audit:client-no-anchor",
            json.dumps(
                {
                    "request_id": "req-skin-audit",
                    "skin_audit": {"primary_observation": "rash"},
                }
            ),
        )
        assert resolve_user_escalation_context("client-no-anchor") is None

        _seed_skin_completed_review(fake_redis, "client-with-anchor")
        fake_redis.set(
            "skin_audit:client-with-anchor",
            json.dumps(
                {
                    "request_id": "req-skin-audit",
                    "skin_audit": {"diagnosis": "inflammatory acne"},
                }
            ),
        )
        context = resolve_user_escalation_context("client-with-anchor")
        assert context is not None
        assert context.audit_case_id == "case-skin-audit"
        assert context.mode == "diagnose"

    def test_product_review_dispatch_uses_same_job_schema(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dermascan.ai_service.postprocessing import escalation as escalation_module

        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        record_completed_review_anchor(
            fake_redis,
            client_id="client-product",
            mode="product_review",
            anchor_request_id="req-product-audit",
            case_id="case-product-audit",
            diagnosis_text="CeraVe Moisturizer",
            review_type="Product review",
            product_name="CeraVe Moisturizer",
            description="Daily moisturizer",
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-product-audit",
            3600,
            json.dumps({"request_id": "req-product-audit", "case_id": "case-product-audit"}),
        )
        status = dispatch_user_requested_escalation(
            "client-product",
            current_request_id="req-turn",
            current_case_id="case-turn",
            message_id="msg-turn",
            channel="whatsapp",
        )
        assert status == "dispatched"
        jobs = list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))
        assert len(jobs) == 1
        fields = jobs[0][1]
        assert fields["mode"] == "product_review"
        assert fields["product_name"] == "CeraVe Moisturizer"
        assert fields["review_type"] == "Product review"
        assert "case_id" in fields
        assert "image_blob_path" in fields


@pytest.mark.unit
class TestDermatologistContextTool:
    def test_readiness_tool(self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from dermascan.backend.services.tools.orchestrator import orchestrator_tools as tools

        monkeypatch.setattr(tools, "_redis", fake_redis)
        fake_redis.setex(
            "orchestrator:ingress_payload:req-turn",
            3600,
            json.dumps(
                {
                    "request_id": "req-turn",
                    "case_id": "case-turn",
                    "client_id": "client-tool",
                    "payload": "{}",
                }
            ),
        )
        result = tools.get_dermatologist_escalation_context.invoke({"request_id": "req-turn"})
        assert result["status"] == "ok"
        assert result["requires_ai_review_first"] is True
        assert result["ready_for_dermatologist"] is False

        _seed_skin_completed_review(fake_redis, "client-tool")
        result_ready = tools.get_dermatologist_escalation_context.invoke({"request_id": "req-turn"})
        assert result_ready["ready_for_dermatologist"] is True
        assert "request_specialist_escalation" in result_ready["instruction"]

"""Unit tests for consent-gated escalation routing (Step 8.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.ai_service.postprocessing import escalation as escalation_module
from dermascan.ai_service.postprocessing.consent_escalation import (
    EscalationDecision,
    append_escalation_offer_to_reply,
    resolve_escalation_decision,
)
from dermascan.ai_service.postprocessing.escalation import (
    _JobEntry,
    dispatch_consent_escalation,
    has_pending_consent_escalation_offer,
    route_case_after_inference,
)
from dermascan.tests.unit.test_medgemma_structured_output import _COSMETIC_PAYLOAD


@pytest.mark.unit
class TestResolveEscalationDecision:
    """Pure routing rules for consent-gated escalation."""

    def test_legacy_low_confidence_escalates(self) -> None:
        decision = resolve_escalation_decision(
            mode="diagnose",
            confidence=0.4,
            threshold=0.75,
            parsed_ok=True,
            risk_tier="cosmetic",
            consent_escalation_active=False,
        )
        assert decision == EscalationDecision(True, False, "legacy_confidence_threshold")

    def test_cosmetic_tier_does_not_auto_escalate_on_low_confidence(self) -> None:
        decision = resolve_escalation_decision(
            mode="diagnose",
            confidence=0.4,
            threshold=0.75,
            parsed_ok=True,
            risk_tier="cosmetic",
            consent_escalation_active=True,
        )
        assert decision.auto_escalate is False
        assert decision.offer_escalation is False

    def test_watch_tier_offers_consent_escalation(self) -> None:
        decision = resolve_escalation_decision(
            mode="diagnose",
            confidence=0.9,
            threshold=0.75,
            parsed_ok=True,
            risk_tier="watch",
            consent_escalation_active=True,
        )
        assert decision.auto_escalate is False
        assert decision.offer_escalation is True

    def test_urgent_tier_still_auto_escalates(self) -> None:
        decision = resolve_escalation_decision(
            mode="diagnose",
            confidence=0.9,
            threshold=0.75,
            parsed_ok=True,
            risk_tier="urgent",
            consent_escalation_active=True,
        )
        assert decision.auto_escalate is True
        assert decision.offer_escalation is False

    def test_product_review_never_auto_escalates_when_consent_mode(self) -> None:
        decision = resolve_escalation_decision(
            mode="product_review",
            confidence=0.2,
            threshold=0.6,
            parsed_ok=True,
            risk_tier="cosmetic",
            consent_escalation_active=True,
        )
        assert decision.auto_escalate is False
        assert decision.offer_escalation is False


@pytest.mark.unit
class TestEscalationOfferCopy:
    """User-facing offer suffix helper."""

    def test_appends_offer_suffix(self) -> None:
        result = append_escalation_offer_to_reply("Your skin looks dry.")
        assert "skin specialist" in result.lower()
        assert "Reply yes" in result

    def test_idempotent_when_already_present(self) -> None:
        text = append_escalation_offer_to_reply("Report.")
        assert append_escalation_offer_to_reply(text) == text


@pytest.mark.unit
class TestRouteCaseConsentEscalation:
    """Integration of consent routing in post-inference escalation."""

    def _entry(self) -> _JobEntry:
        return _JobEntry(
            job_id="job-watch",
            prompt="prompt",
            image_bytes=b"",
            msg_id="msg-watch",
            request_id="req-watch",
            case_id="case-watch",
            client_id="client-watch",
            message_id="msg-watch",
            channel="whatsapp",
            image_blob_path="client-media/wa:123/skin.jpg",
        )

    def test_watch_tier_completes_with_offer_when_flag_on(
        self,
        fake_redis: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        monkeypatch.setattr(
            escalation_module,
            "cosmetic_consent_escalation_active",
            lambda: True,
        )

        escalated = route_case_after_inference(
            self._entry(),
            json.dumps(_COSMETIC_PAYLOAD),
            0.42,
        )
        assert escalated is False
        assert has_pending_consent_escalation_offer("case-watch") is True

        signals = list(fake_redis.xrange(escalation_module._ORCHESTRATOR_SIGNAL_STREAM))
        assert len(signals) == 1
        payload = json.loads(signals[0][1]["payload"])
        assert payload["escalation_offer"] is True
        assert payload["risk_tier"] == "watch"

    def test_dispatch_consent_escalation_pushes_job(
        self,
        fake_redis: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        fake_redis.set(
            "escalation:consent_offer:case-consent",
            json.dumps(
                {
                    "request_id": "req-consent",
                    "case_id": "case-consent",
                    "client_id": "client-consent",
                    "message_id": "msg-consent",
                    "channel": "whatsapp",
                    "mode": "diagnose",
                    "reason": "risk_tier_watch_consent",
                }
            ),
        )

        assert dispatch_consent_escalation("case-consent") is True
        jobs = list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))
        assert len(jobs) == 1
        assert jobs[0][1]["case_id"] == "case-consent"
        assert has_pending_consent_escalation_offer("case-consent") is False


@pytest.mark.unit
class TestOrchestratorConsentTool:
    """request_specialist_escalation tool wiring."""

    def test_dispatch_after_pending_offer(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dermascan.backend.services.tools.orchestrator import orchestrator_tools as tools

        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        monkeypatch.setenv("COSMETIC_CONSENT_ESCALATION", "true")
        monkeypatch.setattr(tools, "_redis", fake_redis)
        monkeypatch.setattr(
            "dermascan.ai_service.postprocessing.escalation._redis",
            fake_redis,
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-consent-tool",
            3600,
            json.dumps(
                {
                    "request_id": "req-consent-tool",
                    "case_id": "case-consent-tool",
                    "client_id": "client-consent-tool",
                    "message_id": "req-consent-tool",
                    "channel": "whatsapp",
                    "payload": "{}",
                }
            ),
        )
        fake_redis.set(
            "escalation:consent_offer:case-consent-tool",
            json.dumps(
                {
                    "request_id": "req-consent-tool",
                    "case_id": "case-consent-tool",
                    "client_id": "client-consent-tool",
                    "message_id": "msg-consent-tool",
                    "channel": "whatsapp",
                    "mode": "diagnose",
                    "reason": "risk_tier_watch_consent",
                }
            ),
        )

        result = tools.request_specialist_escalation.invoke(
            {"request_id": "req-consent-tool"}
        )
        assert result["status"] == "queued"
        assert "specialist" in str(result.get("client_message") or "").lower()


# typing import removed — see top of file

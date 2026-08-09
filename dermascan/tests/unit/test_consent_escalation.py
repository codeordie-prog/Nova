"""Unit tests for consent-gated escalation routing (Step 8.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.ai_service.postprocessing import escalation as escalation_module
from dermascan.ai_service.postprocessing.consent_escalation import (
    EscalationDecision,
    append_escalation_offer_to_reply,
    resolve_consent_escalation_decision,
    resolve_escalation_decision,
    should_apply_watch_tier_guardrail,
)
from dermascan.ai_service.postprocessing.escalation import (
    _JobEntry,
    dispatch_consent_escalation,
    dispatch_user_requested_escalation,
    has_pending_consent_escalation_offer,
    resolve_user_escalation_context,
    route_case_after_inference,
)
from dermascan.shared.intake_session import IntakeImageCategory, IntakeImageRef, IntakeSession, ServiceIntent
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
class TestWatchTierGuardrail:
    """Deterministic bump from cosmetic to consent-gated watch offer."""

    _BASE_FIELDS: dict[str, Any] = {
        "diagnosis": "inflammatory acne appearance",
        "primary_observation": "papules on cheeks",
        "skin_state": {
            "severity": "moderate",
            "active_inflammation": True,
            "observed_patterns": ["papules"],
        },
    }

    def test_guardrail_fires_for_moderate_inflammation_untreated(self) -> None:
        assert should_apply_watch_tier_guardrail(
            mode="diagnose",
            risk_tier="cosmetic",
            base_fields=self._BASE_FIELDS,
            audit_context="Current medications: none",
        )

    def test_guardrail_skips_when_on_treatment(self) -> None:
        assert not should_apply_watch_tier_guardrail(
            mode="diagnose",
            risk_tier="cosmetic",
            base_fields=self._BASE_FIELDS,
            audit_context="User uses tretinoin nightly",
        )

    def test_guardrail_skips_for_product_review_mode(self) -> None:
        assert not should_apply_watch_tier_guardrail(
            mode="product_review",
            risk_tier="cosmetic",
            base_fields=self._BASE_FIELDS,
            audit_context="Current medications: none",
        )

    def test_guardrail_skips_when_model_already_watch(self) -> None:
        assert not should_apply_watch_tier_guardrail(
            mode="diagnose",
            risk_tier="watch",
            base_fields=self._BASE_FIELDS,
            audit_context="Current medications: none",
        )

    def test_resolve_consent_decision_applies_guardrail(self) -> None:
        decision = resolve_consent_escalation_decision(
            mode="diagnose",
            confidence=0.9,
            threshold=0.75,
            parsed_ok=True,
            risk_tier="cosmetic",
            consent_escalation_active=True,
            base_fields=self._BASE_FIELDS,
            audit_context="No current treatment",
        )
        assert decision == EscalationDecision(
            False,
            True,
            "watch_guardrail_inflammatory_untreated",
        )


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
        assert has_pending_consent_escalation_offer("client-watch") is True

        signals = list(fake_redis.xrange(escalation_module._ORCHESTRATOR_SIGNAL_STREAM))
        assert len(signals) == 1
        payload = json.loads(signals[0][1]["payload"])
        assert payload["escalation_offer"] is True
        assert payload["risk_tier"] == "watch"

    def test_guardrail_bumps_cosmetic_to_watch_offer(
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

        payload = dict(_COSMETIC_PAYLOAD)
        payload["risk_tier"] = "cosmetic"
        entry = self._entry()
        entry.prompt = "Current medications: none"

        escalated = route_case_after_inference(
            entry,
            json.dumps(payload),
            0.9,
        )
        assert escalated is False
        assert has_pending_consent_escalation_offer("client-watch") is True

        signals = list(fake_redis.xrange(escalation_module._ORCHESTRATOR_SIGNAL_STREAM))
        payload_out = json.loads(signals[0][1]["payload"])
        assert payload_out["escalation_offer"] is True
        assert payload_out["risk_tier"] == "watch"

    def test_dispatch_consent_escalation_pushes_job(
        self,
        fake_redis: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        fake_redis.set(
            "escalation:consent_offer:client:client-consent",
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

        assert dispatch_consent_escalation("client-consent") is True
        jobs = list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))
        assert len(jobs) == 1
        assert jobs[0][1]["case_id"] == "case-consent"
        assert has_pending_consent_escalation_offer("client-consent") is False

    def test_legacy_case_key_still_dispatches(
        self,
        fake_redis: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        fake_redis.set(
            "escalation:consent_offer:case-legacy",
            json.dumps(
                {
                    "request_id": "req-legacy",
                    "case_id": "case-legacy",
                    "client_id": "client-legacy",
                    "message_id": "msg-legacy",
                    "channel": "whatsapp",
                    "mode": "diagnose",
                    "reason": "risk_tier_watch_consent",
                }
            ),
        )

        assert has_pending_consent_escalation_offer(
            "client-legacy",
            case_id="case-legacy",
        ) is True
        assert dispatch_consent_escalation(
            "client-legacy",
            case_id="case-legacy",
        ) is True


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
                    "case_id": "case-consent-tool-new-turn",
                    "client_id": "client-consent-tool",
                    "message_id": "req-consent-tool",
                    "channel": "whatsapp",
                    "payload": "{}",
                }
            ),
        )
        fake_redis.set(
            "escalation:consent_offer:client:client-consent-tool",
            json.dumps(
                {
                    "request_id": "req-skin-audit",
                    "case_id": "case-skin-audit",
                    "client_id": "client-consent-tool",
                    "message_id": "msg-skin-audit",
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
        assert result["case_id"] == "case-consent-tool-new-turn"
        jobs = list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))
        assert len(jobs) == 1
        assert jobs[0][1]["case_id"] == "case-skin-audit"
        assert "specialist" in str(result.get("client_message") or "").lower()

    def test_user_requested_dispatch_without_pending_offer(
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
        monkeypatch.setattr(
            "dermascan.shared.skin_audit_store.get_redis_client",
            lambda decode_responses=True: fake_redis,
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-user-escalation",
            3600,
            json.dumps(
                {
                    "request_id": "req-user-escalation",
                    "case_id": "case-user-turn",
                    "client_id": "client-user-escalation",
                    "message_id": "msg-user-turn",
                    "channel": "whatsapp",
                    "payload": "{}",
                }
            ),
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-skin-audit",
            3600,
            json.dumps(
                {
                    "request_id": "req-skin-audit",
                    "case_id": "case-skin-audit",
                    "client_id": "client-user-escalation",
                    "message_id": "msg-skin-audit",
                    "channel": "whatsapp",
                    "payload": json.dumps(
                        {
                            "attachments": [
                                {
                                    "type": "image",
                                    "url": "https://storage.example/client-media/wa:1/skin.jpg",
                                }
                            ]
                        }
                    ),
                }
            ),
        )
        fake_redis.set(
            "skin_audit:client-user-escalation",
            json.dumps(
                {
                    "schema_version": "cosmetic_v1",
                    "request_id": "req-skin-audit",
                    "updated_at_ts": 1_700_000_000,
                    "skin_audit": {
                        "primary_observation": "moderate inflammatory acne appearance",
                        "primary_concern": "moderate inflammatory acne appearance",
                        "risk_tier": "cosmetic",
                        "confidence": 0.82,
                        "current_condition": {
                            "severity": "moderate",
                            "observed_patterns": ["papules"],
                        },
                    },
                }
            ),
        )

        session = IntakeSession(
            client_id="client-user-escalation",
            case_id="case-user-turn",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Acne on cheeks",
            reproductive_status_confirmed=True,
            images={
                IntakeImageCategory.SKIN.value: IntakeImageRef(
                    request_id="req-skin-audit",
                    image_url="https://storage.example/client-media/wa:1/skin.jpg",
                ),
            },
        )
        monkeypatch.setattr(
            tools,
            "load_intake_session",
            lambda client_id, redis_client=None: session,
        )
        monkeypatch.setattr(
            tools,
            "_load_skin_profile_mapping",
            lambda client_id: {
                "current_medications": [],
                "confirmed_none_fields": ["current_medications"],
            },
        )

        result = tools.request_specialist_escalation.invoke(
            {"request_id": "req-user-escalation"}
        )
        assert result["status"] == "queued"
        assert result.get("dispatch_path") == "user_requested"
        jobs = list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))
        assert len(jobs) == 1
        assert jobs[0][1]["case_id"] == "case-skin-audit"
        assert jobs[0][1]["reason"] == "user_requested_specialist"
        assert "inflammatory acne" in jobs[0][1]["diagnosis_text"]

    def test_user_requested_missing_skin_context(
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
            "orchestrator:ingress_payload:req-no-skin",
            3600,
            json.dumps(
                {
                    "request_id": "req-no-skin",
                    "case_id": "case-no-skin",
                    "client_id": "client-no-skin",
                    "message_id": "msg-no-skin",
                    "channel": "whatsapp",
                    "payload": "{}",
                }
            ),
        )
        monkeypatch.setattr(
            tools,
            "load_intake_session",
            lambda client_id, redis_client=None: None,
        )
        monkeypatch.setattr(tools, "_load_skin_profile_mapping", lambda client_id: {})

        result = tools.request_specialist_escalation.invoke({"request_id": "req-no-skin"})
        assert result["status"] == "missing_skin_context"
        assert len(list(fake_redis.xrange(escalation_module._ESCALATION_STREAM))) == 0


@pytest.mark.unit
class TestUserRequestedEscalationBuilder:
    """On-demand escalation job builder (Step 8.2d)."""

    def test_resolve_context_from_skin_audit(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        monkeypatch.setattr(
            "dermascan.shared.skin_audit_store.get_redis_client",
            lambda decode_responses=True: fake_redis,
        )
        fake_redis.set(
            "skin_audit:client-builder",
            json.dumps(
                {
                    "request_id": "req-audit",
                    "skin_audit": {
                        "primary_observation": "dry patches",
                        "risk_tier": "cosmetic",
                    },
                }
            ),
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-audit",
            3600,
            json.dumps({"request_id": "req-audit", "case_id": "case-audit"}),
        )

        context = resolve_user_escalation_context("client-builder")
        assert context is not None
        assert context.audit_case_id == "case-audit"
        assert context.diagnosis_text == "dry patches"

    def test_dispatch_is_idempotent(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(escalation_module, "_redis", fake_redis)
        monkeypatch.setattr(
            "dermascan.shared.skin_audit_store.get_redis_client",
            lambda decode_responses=True: fake_redis,
        )
        fake_redis.set(
            "escalation:user_requested:client:client-dedupe",
            "req-old",
        )
        fake_redis.set(
            "skin_audit:client-dedupe",
            json.dumps(
                {
                    "request_id": "req-audit",
                    "skin_audit": {"primary_observation": "rash"},
                }
            ),
        )
        fake_redis.setex(
            "orchestrator:ingress_payload:req-audit",
            3600,
            json.dumps({"request_id": "req-audit", "case_id": "case-audit"}),
        )

        assert (
            dispatch_user_requested_escalation(
                "client-dedupe",
                current_request_id="req-new",
                current_case_id="case-new",
                message_id="msg-new",
                channel="whatsapp",
            )
            == "already_dispatched"
        )

"""Unit tests for orchestrator intake tools."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.backend.services.tools.orchestrator import orchestrator_tools as tools
from dermascan.shared.intake_session import IntakePhase, IntakeSession, ServiceIntent, recompute_session
from dermascan.shared.intake_session_store import load_intake_session


_FULL_PROFILE: dict[str, Any] = {
    "age": 25,
    "gender": "female",
    "location": "Nairobi",
    "current_products": ["cleanser"],
    "current_goals": ["hydration"],
    "allergies": ["none"],
    "pregnancy": False,
    "breastfeeding": False,
}


@pytest.fixture(autouse=True)
def _disable_cosmetic_flags_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy routing unless a test explicitly enables cosmetic flags."""
    monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "false")
    monkeypatch.setenv("COSMETIC_SKIN_FIRST_ROUTING", "false")
    monkeypatch.setenv("COSMETIC_TEXT_PRODUCT_REVIEW", "false")


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

    def test_update_intake_empty_list_confirms_none_and_routes(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        profile = {**_FULL_PROFILE, "current_products": []}
        monkeypatch.setattr(tools, "_fetch_skin_profile_dict", lambda _cid: dict(profile))
        monkeypatch.setattr(
            tools, "_persist_skin_profile", lambda _cid, _updates: (dict(profile), None)
        )
        self._cache_envelope(fake_redis, "req-none", "client-none", "case-none")

        tools.update_intake.invoke(
            {
                "request_id": "req-none",
                "service_intent": "skin_review",
                "primary_concern": "Bumps on back",
                "confirm_reproductive_status": True,
            }
        )
        tools.register_intake_image.invoke(
            {"request_id": "req-none", "image_category": "skin"}
        )

        status = tools.update_intake.invoke({"request_id": "req-none"})
        assert not status["ready_to_route"]
        assert "current_products" in status["profile_gaps"]

        result = tools.update_intake.invoke(
            {"request_id": "req-none", "current_products": []}
        )
        assert result["status"] == "saved"
        assert "confirmed_none_fields" in result["updated_fields"]
        assert "current_products" not in result["profile_gaps"]
        assert result["ready_to_route"]

    def test_post_turn_dispatch_when_model_skips_enqueue(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-auto", "client-auto", "case-auto")

        dispatched: list[dict[str, Any]] = []

        def _fake_dispatch(**kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued", "medgemma_job_id": "job-auto"}

        monkeypatch.setattr(tools, "_dispatch_audit_image_ref", _fake_dispatch)
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)
        monkeypatch.setattr(
            tools,
            "_merge_auto_dispatch_into_intake_result",
            lambda result, **kwargs: result,
        )

        self._complete_product_intake("req-auto")

        results = tools.ensure_ready_intake_dispatched("client-auto", "req-auto")
        assert [item["status"] for item in results] == ["queued"]
        assert len(dispatched) == 1

        session = load_intake_session("client-auto", redis_client=fake_redis)
        assert session is not None
        assert session.phase == IntakePhase.AWAITING_AUDIT

        assert tools.ensure_ready_intake_dispatched("client-auto", "req-auto") == []

    def test_auto_dispatch_on_register_when_ready(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(
            fake_redis, "req-skin-auto", "client-skin-auto", "case-skin-auto", "https://example.com/skin.jpg"
        )

        dispatched: list[dict[str, Any]] = []

        def _fake_dispatch(**kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued", "medgemma_job_id": "job-skin-auto"}

        monkeypatch.setattr(tools, "_dispatch_audit_image_ref", _fake_dispatch)
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)

        tools.update_intake.invoke(
            {
                "request_id": "req-skin-auto",
                "service_intent": "skin_review",
                "primary_concern": "Cheek rash",
                "confirm_reproductive_status": True,
                "current_medications": [],
            }
        )
        registered = tools.register_intake_image.invoke(
            {"request_id": "req-skin-auto", "image_category": "skin"}
        )
        assert registered["status"] == "registered"
        assert registered.get("auto_dispatched") == ["queued"]
        assert registered["phase"] == IntakePhase.AWAITING_AUDIT.value
        assert len(dispatched) == 1
        assert "skin" in dispatched[0].get("context", "").lower() or dispatched[0].get("image_url")

    def test_new_image_on_awaiting_audit_session_reopens_and_dispatches(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh photo on a stuck awaiting_audit session must be re-enqueued.

        Regression: infer_phase keeps awaiting_audit sticky, so a new review on
        the same client previously reported 'already queued' and never dispatched.
        """
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(
            fake_redis, "req-old", "client-reopen", "case-reopen", "https://example.com/old.jpg"
        )
        self._cache_envelope(
            fake_redis, "req-new", "client-reopen", "case-reopen", "https://example.com/new.jpg"
        )

        dispatched: list[dict[str, Any]] = []

        def _fake_dispatch(**kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued", "medgemma_job_id": f"job-{len(dispatched)}"}

        monkeypatch.setattr(tools, "_dispatch_audit_image_ref", _fake_dispatch)
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)

        # First review: completes and lands in awaiting_audit.
        tools.update_intake.invoke(
            {
                "request_id": "req-old",
                "service_intent": "skin_review",
                "primary_concern": "Cheek rash",
                "confirm_reproductive_status": True,
                "current_medications": [],
            }
        )
        tools.register_intake_image.invoke(
            {"request_id": "req-old", "image_category": "skin"}
        )
        session = load_intake_session("client-reopen", redis_client=fake_redis)
        assert session is not None
        assert session.phase == IntakePhase.AWAITING_AUDIT
        assert len(dispatched) == 1

        # New photo arrives while the session is still awaiting_audit.
        registered = tools.register_intake_image.invoke(
            {"request_id": "req-new", "image_category": "skin", "visual_note": "New rash photo"}
        )
        assert registered["status"] == "registered"
        assert registered.get("auto_dispatched") == ["queued"]
        assert len(dispatched) == 2, "new image must be auto-dispatched"
        assert dispatched[1]["image_url"] == "https://example.com/new.jpg"

        session = load_intake_session("client-reopen", redis_client=fake_redis)
        assert session is not None
        assert session.phase == IntakePhase.AWAITING_AUDIT

        # Re-sending the same request must stay deduped.
        assert tools.ensure_ready_intake_dispatched("client-reopen", "req-new") == []
        assert len(dispatched) == 2

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
        assert result["status"] == "already_queued"
        assert result["audit_type"] == "product"
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

        assert len(dispatched) == 2
        assert dispatched[0]["group_id"] == "case-combo"
        assert dispatched[0]["group_expected_count"] == 2
        assert dispatched[1]["group_id"] == "case-combo"
        assert dispatched[1]["group_expected_count"] == 2

        skin_result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-skin", "audit_type": "skin"}
        )
        assert skin_result["status"] == "already_queued"

        session_mid = load_intake_session("client-combo", redis_client=fake_redis)
        assert session_mid is not None
        assert session_mid.phase == IntakePhase.AWAITING_AUDIT

        product_result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-skin", "audit_type": "product"}
        )
        assert product_result["status"] == "already_queued"
        assert len(dispatched) == 2

        session_final = load_intake_session("client-combo", redis_client=fake_redis)
        assert session_final is not None
        assert session_final.phase == IntakePhase.AWAITING_AUDIT

    def test_update_intake_skin_snapshot_skipped_sets_generic_mode(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-skip", "client-skip", "case-skip")

        result = tools.update_intake.invoke(
            {
                "request_id": "req-skip",
                "service_intent": "product_review",
                "skin_snapshot_skipped": True,
            }
        )
        assert result["status"] == "saved"
        assert result["skin_snapshot_skipped"] is True
        assert result["personalization_mode"] == "generic"
        assert "skin_snapshot_skipped" in result["updated_fields"]

        session = load_intake_session("client-skip", redis_client=fake_redis)
        assert session is not None
        assert session.skin_snapshot_skipped is True
        assert session.personalization_mode == "generic"

    def test_update_intake_routing_unchanged_when_skin_skipped(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step 1.2: skip flag is stored but routing still requires product image."""
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-skip2", "client-skip2", "case-skip2")

        self._complete_product_intake("req-skip2")
        result = tools.update_intake.invoke(
            {"request_id": "req-skip2", "skin_snapshot_skipped": True}
        )
        assert result["skin_snapshot_skipped"] is True
        assert result["ready_to_route"] is True

    def test_combined_missing_skin_instruction_offers_opt_out(self) -> None:
        session = recompute_session(
            IntakeSession(
                client_id="client-combined-opt",
                case_id="case-combined-opt",
                service_intent=ServiceIntent.COMBINED,
                phase=IntakePhase.IMAGE_INTAKE,
                primary_concern="check product for my skin",
                reproductive_status_confirmed=True,
            ),
            _FULL_PROFILE,
        )
        instruction = tools._intake_instruction(session, _FULL_PROFILE)
        assert "skin_snapshot_skipped=true" in instruction
        assert "product_review" in instruction

    def test_skip_flag_instruction_suppresses_skin_photo_requests(self) -> None:
        session = recompute_session(
            IntakeSession(
                client_id="client-skipped",
                case_id="case-skipped",
                service_intent=ServiceIntent.PRODUCT_REVIEW,
                phase=IntakePhase.IMAGE_INTAKE,
                primary_concern="is this serum safe",
                reproductive_status_confirmed=True,
                skin_snapshot_skipped=True,
                personalization_mode="generic",
            ),
            _FULL_PROFILE,
        )
        instruction = tools._intake_instruction(session, _FULL_PROFILE)
        assert "do NOT ask for a skin photo" in instruction

    def test_skin_first_instruction_requires_snapshot(self) -> None:
        session = recompute_session(
            IntakeSession(
                client_id="client-skin-first",
                case_id="case-skin-first",
                service_intent=ServiceIntent.PRODUCT_REVIEW,
                phase=IntakePhase.IMAGE_INTAKE,
                primary_concern="is this serum safe",
                reproductive_status_confirmed=True,
                product_ref_text="CeraVe Retinol Serum",
            ),
            _FULL_PROFILE,
            cosmetic_pivot_enabled=True,
            cosmetic_skin_first_routing=True,
            skin_audit={},
            now_ms=1_700_000_000 * 1000,
        )
        instruction = tools._intake_instruction(session, _FULL_PROFILE)
        assert "Fresh skin snapshot required" in instruction
        assert "skin_snapshot_skipped=true" in instruction

    def test_update_intake_product_ref_text_when_pivot_enabled(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        monkeypatch.setenv("COSMETIC_TEXT_PRODUCT_REVIEW", "true")
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-ref", "client-ref", "case-ref")

        result = tools.update_intake.invoke(
            {
                "request_id": "req-ref",
                "service_intent": "product_review",
                "skin_snapshot_skipped": True,
                "product_ref_text": "CeraVe Moisturizing Cream",
                "primary_concern": "Is this safe?",
                "confirm_reproductive_status": True,
            }
        )
        assert result["product_ref_text"] == "CeraVe Moisturizing Cream"
        assert result["ready_to_route"] is True
        assert "product_ref_text" in result["updated_fields"]
        assert result.get("auto_dispatched") or "text-only product check" in result["instruction"]

        session = load_intake_session("client-ref", redis_client=fake_redis)
        assert session is not None
        assert session.product_ref_text == "CeraVe Moisturizing Cream"

    def test_enqueue_text_product_review_without_image(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        monkeypatch.setenv("COSMETIC_TEXT_PRODUCT_REVIEW", "true")
        monkeypatch.setattr(tools, "_redis", fake_redis)
        self._wire_profile(monkeypatch)
        self._cache_envelope(fake_redis, "req-text-enq", "client-text-enq", "case-text-enq")

        update_result = tools.update_intake.invoke(
            {
                "request_id": "req-text-enq",
                "service_intent": "product_review",
                "skin_snapshot_skipped": True,
                "product_ref_text": "The Ordinary Niacinamide 10%",
                "primary_concern": "Is this safe?",
                "confirm_reproductive_status": True,
            }
        )

        if update_result.get("auto_dispatched"):
            result = update_result
        else:
            result = tools.enque_request_for_auditing_stream.invoke(
                {"request_id": "req-text-enq", "audit_type": "product"}
            )
        assert result.get("auto_dispatched") or result["status"] in {"queued", "already_queued"}
        assert result.get("text_only") is True or update_result.get("phase") == IntakePhase.AWAITING_AUDIT.value

        session = load_intake_session("client-text-enq", redis_client=fake_redis)
        assert session is not None
        assert session.phase == IntakePhase.AWAITING_AUDIT


@pytest.mark.unit
class TestSkinFirstPrerequisiteChain:
    """Skin prerequisite enqueue and auto-chain to product review (Step 6.1b)."""

    def test_skin_prerequisite_enqueue_when_snapshot_stale(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        monkeypatch.setenv("COSMETIC_SKIN_FIRST_ROUTING", "true")
        monkeypatch.setenv("COSMETIC_TEXT_PRODUCT_REVIEW", "true")
        monkeypatch.setattr(tools, "_redis", fake_redis)
        intake = TestIntakeTools()
        intake._wire_profile(monkeypatch)
        intake._cache_envelope(
            fake_redis,
            "req-skin-pre",
            "client-skin-pre",
            "case-skin-pre",
            image_url="https://example.com/skin.jpg",
        )

        tools.update_intake.invoke(
            {
                "request_id": "req-skin-pre",
                "service_intent": "product_review",
                "product_ref_text": "Test Serum",
                "primary_concern": "Is this safe?",
                "confirm_reproductive_status": True,
                "current_medications": [],
            }
        )

        registered = tools.register_intake_image.invoke(
            {"request_id": "req-skin-pre", "image_category": "skin"}
        )
        assert registered["status"] == "registered"
        assert "skin_snapshot_stale" in registered["missing_for_routing"]

        monkeypatch.setattr(
            tools,
            "_dispatch_audit_image_ref",
            lambda **_kwargs: {"status": "queued", "medgemma_job_id": "job-skin-pre"},
        )
        monkeypatch.setattr(tools, "_emit_orchestrator_event", lambda _fields: None)

        result = tools.enque_request_for_auditing_stream.invoke(
            {"request_id": "req-skin-pre", "audit_type": "skin"}
        )
        assert result["status"] == "queued"
        assert result["audit_type"] == "skin"
        assert "automatically" in str(result.get("instruction") or "").lower()

        session = load_intake_session("client-skin-pre", redis_client=fake_redis)
        assert session is not None
        assert session.pending_product_after_skin is True
        assert session.phase == IntakePhase.AWAITING_AUDIT

    def test_continue_product_after_skin_prerequisite_dispatches_text(
        self, fake_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COSMETIC_PIVOT_ENABLED", "true")
        monkeypatch.setenv("COSMETIC_SKIN_FIRST_ROUTING", "true")
        monkeypatch.setenv("COSMETIC_TEXT_PRODUCT_REVIEW", "true")
        monkeypatch.setattr(tools, "_redis", fake_redis)
        monkeypatch.setattr(
            "dermascan.shared.skin_audit_store.get_redis_client",
            lambda **_kwargs: fake_redis,
        )
        intake = TestIntakeTools()
        intake._wire_profile(monkeypatch)

        from dermascan.shared.intake_session_store import save_intake_session_recomputed
        from dermascan.shared.skin_audit_store import persist_skin_audit_snapshot

        session = IntakeSession(
            client_id="client-chain",
            case_id="case-chain",
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            primary_concern="Is this safe?",
            reproductive_status_confirmed=True,
            product_ref_text="Chain Serum",
            pending_product_after_skin=True,
            phase=IntakePhase.AWAITING_AUDIT,
            confirmed_none_fields=["current_medications"],
        )
        save_intake_session_recomputed(session, _FULL_PROFILE, redis_client=fake_redis)
        persist_skin_audit_snapshot(
            "client-chain",
            skin_audit={"primary_concern": "visible dryness"},
            request_id="skin-req",
        )

        dispatched: list[dict[str, Any]] = []

        def _fake_text_dispatch(*_args: Any, **kwargs: Any) -> dict[str, str]:
            dispatched.append(kwargs)
            return {"status": "queued"}

        monkeypatch.setattr(tools, "dispatch_text_product_review_signal", _fake_text_dispatch)

        ok = tools.continue_product_review_after_skin_prerequisite(
            "client-chain",
            "case-chain",
            request_id="skin-signal-req",
        )
        assert ok is True
        assert len(dispatched) == 1
        assert dispatched[0]["session"].product_ref_text == "Chain Serum"

        session_after = load_intake_session("client-chain", redis_client=fake_redis)
        assert session_after is not None
        assert session_after.pending_product_after_skin is False
        assert session_after.phase == IntakePhase.AWAITING_AUDIT


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

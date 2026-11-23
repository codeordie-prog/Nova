"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Snapshot tests for orchestrator cosmetic positioning (Step 4.1)."""


from __future__ import annotations

import pytest

from dermascan.backend.services.agents.prompts import orchestrator_prompt


@pytest.mark.unit
class TestOrchestratorPromptCosmeticPositioning:
    """Prompt exposes cosmetic-intelligence framing without changing tool contracts."""

    def test_prompt_mentions_cosmetic_decision_support(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "cosmetic decision support" in prompt.lower()
        assert "medical diagnosis" in prompt.lower()
        assert "not" in prompt.lower() and "prescription service" in prompt.lower()

    def test_prompt_reframes_menu_options(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "Skin snapshot" in prompt
        assert "Product check" in prompt

    def test_prompt_keeps_legacy_tool_and_intent_names(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "service_intent skin_review" in prompt or "skin_review" in prompt
        assert "product_review" in prompt
        assert "update_intake" in prompt
        assert "enque_request_for_auditing_stream" in prompt
        assert "register_intake_image" in prompt

    def test_prompt_mentions_optional_skip_copy_without_tool_change(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "skin snapshot opt-out" in prompt.lower()
        assert "skin_snapshot_skipped=true" in prompt.lower()
        assert "explicit consent" in prompt.lower()

    def test_prompt_opt_out_does_not_remove_product_image_requirement(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "product label photo" in prompt.lower()
        assert "product_ref_text" in prompt.lower()

    def test_prompt_mentions_skin_first_routing(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "skin-first" in prompt.lower()
        assert "skin_snapshot_stale" in prompt.lower()
        assert "audit_type=skin" in prompt.lower()

    def test_prompt_mentions_consent_escalation_tool(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "request_specialist_escalation" in prompt.lower()
        assert "explicit" in prompt.lower()

    def test_prompt_mentions_reminder_tools(self) -> None:
        prompt = orchestrator_prompt.system_prompt
        assert "list_routines" in prompt
        assert "set_morning_brief" in prompt
        assert "24 hours" in prompt.lower()
        assert "local morning" in prompt.lower()

    def test_prompt_routes_text_product_review_through_enqueue_not_search(self) -> None:
        prompt = orchestrator_prompt.system_prompt.lower()
        assert "text-based product review" in prompt
        assert "internet_search" in prompt
        assert "audit_type=product" in prompt
        assert "do not use internet_search" in prompt or "never" in prompt

    def test_prompt_product_photo_uses_text_path_not_register_product_image(self) -> None:
        prompt = orchestrator_prompt.system_prompt.lower()
        assert "do not" in prompt and "register_intake_image" in prompt
        assert "image_category=product" in prompt or "category=product" in prompt
        assert "medgemma" in prompt
        assert "product_ref_text" in prompt

    def test_prompt_product_review_rules_engine_not_medgemma(self) -> None:
        prompt = orchestrator_prompt.system_prompt.lower()
        assert "product review invariant" in prompt
        assert "rules engine" in prompt
        assert "never enqueued for products" in prompt or "never used for product" in prompt
        assert "audit_type=product" in prompt

    def test_prompt_gates_dermatologist_offer_on_skin_analysis(self) -> None:
        prompt = orchestrator_prompt.system_prompt.lower()
        assert "completed ai skin snapshot" in prompt or "skin snapshot ai review" in prompt
        assert "product-only" in prompt
        assert "dermatologist gate" in prompt
        assert "never call request_specialist_escalation before" in prompt
        assert "skin analysis first" in prompt or "skin snapshot ai review first" in prompt

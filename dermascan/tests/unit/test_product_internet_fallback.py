"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for product review internet fallback heuristics."""


from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

_knowledge_agent_stub = ModuleType("dermascan.rules_engine.knowledge_agent.knowledge_agent")
_knowledge_agent_stub.create_knowledge_agent = MagicMock()
_knowledge_agent_stub.init_model_with_tools = MagicMock()
_knowledge_agent_stub.internet_search = MagicMock()
sys.modules["dermascan.rules_engine.knowledge_agent.knowledge_agent"] = _knowledge_agent_stub

_vector_store_stub = ModuleType("dermascan.rules_engine.vector_store.build_vectorstore")
_vector_store_stub.VectorStore = MagicMock
sys.modules["dermascan.rules_engine.vector_store.build_vectorstore"] = _vector_store_stub

from dermascan.rules_engine.engine.engine import IngredientIndexResult, RulesEngineRequest  # noqa: E402
from dermascan.rules_engine.engine.rules_engine import RulesEngine  # noqa: E402


@pytest.fixture
def engine() -> RulesEngine:
    return RulesEngine(redis_client=MagicMock(), vector_store=MagicMock())


@pytest.mark.unit
class TestProductInternetFallback:
    """Fallback trigger conditions."""

    def test_partial_index_outcome_triggers_fallback(self, engine: RulesEngine) -> None:
        """Partial indexing always supplements with internet search."""
        request = RulesEngineRequest(
            request_id="r1",
            case_id="c1",
            client_id="u1",
            message_id="m1",
            channel="whatsapp",
            ingredients=["Niacinamide"],
            product_name="Test Serum",
        )
        report = {"safety": [], "contraindications": [], "irritation": []}
        assert engine._should_use_product_internet_fallback(
            request,
            indexed_results=[],
            ingredient_ids=[],
            index_outcome="partial",
            report=report,
        )

    def test_empty_vector_evidence_triggers_fallback(self, engine: RulesEngine) -> None:
        """No CIR matches with ingredients present triggers fallback."""
        request = RulesEngineRequest(
            request_id="r1",
            case_id="c1",
            client_id="u1",
            message_id="m1",
            channel="whatsapp",
            ingredients=["Niacinamide"],
            product_name="Test Serum",
        )
        report = {"safety": [], "contraindications": [], "irritation": []}
        assert engine._should_use_product_internet_fallback(
            request,
            indexed_results=[
                IngredientIndexResult(
                    ingredient="Niacinamide",
                    normalized_name="niacinamide",
                    indexed=True,
                    source="redis_cache",
                    ingredient_id="id-1",
                )
            ],
            ingredient_ids=["id-1"],
            index_outcome="success",
            report=report,
        )

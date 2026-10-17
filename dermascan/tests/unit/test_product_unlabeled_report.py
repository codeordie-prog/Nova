"""Unit tests for best-effort product reports when INCI is missing."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

os_environ = __import__("os").environ
os_environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

_knowledge_agent_stub = ModuleType("dermascan.rules_engine.knowledge_agent.knowledge_agent")
_knowledge_agent_stub.create_knowledge_agent = MagicMock()
_knowledge_agent_stub.init_model_with_tools = MagicMock()
_knowledge_agent_stub.internet_search = MagicMock()
sys.modules["dermascan.rules_engine.knowledge_agent.knowledge_agent"] = _knowledge_agent_stub

_vector_store_stub = ModuleType("dermascan.rules_engine.vector_store.build_vectorstore")
_vector_store_stub.VectorStore = MagicMock
sys.modules["dermascan.rules_engine.vector_store.build_vectorstore"] = _vector_store_stub

from dermascan.backend.services.agents import product_rules_signal_helper as helper  # noqa: E402


@pytest.mark.unit
class TestUnlabeledProductFlow:
    """Empty ingredient extraction uses best-effort report path."""

    def test_empty_ingredients_calls_best_effort_builder(self) -> None:
        fields = {
            "request_id": "req-1",
            "case_id": "case-1",
            "client_id": "c-1",
            "message_id": "m-1",
            "channel": "telegram",
            "payload": '{"product_name": "Cetaphil Cleanser", "description": ""}',
        }
        with patch.object(helper, "load_delivery_context", return_value=MagicMock(skin_profile=None)):
            with patch.object(helper, "build_rules_skin_context", return_value={}):
                with patch.object(helper, "_load_latest_skin_audit", return_value=({}, None)):
                    with patch.object(helper, "merge_profile_into_skin_audit", side_effect=lambda a, _b: a):
                        with patch.object(helper, "resolve_product_personalization_mode", return_value="personalized"):
                            with patch.object(helper, "is_skin_snapshot_fresh", return_value=False):
                                with patch.object(
                                    helper,
                                    "extract_product_ingredients",
                                    return_value=helper.IngredientExtractionResponse(ingredients=[]),
                                ):
                                    with patch.object(
                                        helper,
                                        "build_best_effort_product_report_without_inci",
                                        return_value="Cetaphil Cleanser: Safety Review\nFunction: cleanser",
                                    ) as best_effort:
                                        result = helper.run_product_signal_rules_flow(fields, [])

        best_effort.assert_called_once()
        assert result["ingredients_unreadable"] is True
        assert "Safety Review" in result["user_friendly_report"]
        assert result["user_friendly_report"] != helper.PRODUCT_LABEL_UNREADABLE_MESSAGE

"""Unit tests for async vs inline ingredient indexing in RulesEngine."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from dermascan.backend.services.agents import rules_index_jobs

_knowledge_agent_stub = ModuleType("dermascan.rules_engine.knowledge_agent.knowledge_agent")
_knowledge_agent_stub.create_knowledge_agent = MagicMock()
_knowledge_agent_stub.init_model_with_tools = MagicMock()
_knowledge_agent_stub.internet_search = MagicMock()
sys.modules["dermascan.rules_engine.knowledge_agent.knowledge_agent"] = _knowledge_agent_stub

_vector_store_stub = ModuleType("dermascan.rules_engine.vector_store.build_vectorstore")
_vector_store_stub.VectorStore = MagicMock
sys.modules["dermascan.rules_engine.vector_store.build_vectorstore"] = _vector_store_stub

from dermascan.rules_engine.engine.rules_engine import RulesEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture
def rules_engine() -> RulesEngine:
    """RulesEngine with external dependencies mocked."""
    return RulesEngine(redis_client=MagicMock(), vector_store=MagicMock())


@pytest.mark.unit
class TestEnsureReportsExistsAsync:
    """Queue path when RULES_INDEX_INLINE=false."""

    def test_async_enqueues_and_registers_batch(
        self,
        rules_engine: RulesEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing cache + report row enqueues job and returns source=queued."""
        monkeypatch.setenv("RULES_INDEX_INLINE", "false")
        row = {
            "ingredient_id": "ing-1",
            "download_url": "https://example.com/a.pdf",
            "cir_ingredient_name": "Niacinamide",
            "function": "active",
            "report_url": "https://cir.example/r",
            "attachment_url": "",
            "storage_path": "cir-pdfs/a.pdf",
        }
        rules_engine._fetch_report_rows = MagicMock(return_value={"niacinamide": row})  # type: ignore[method-assign]
        rules_engine._is_cached = MagicMock(return_value=False)  # type: ignore[method-assign]

        schedule = rules_index_jobs.RulesIndexScheduleContext(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
        )

        with patch(
            "dermascan.rules_engine.engine.rules_engine.enqueue_rules_index_job",
        ) as enqueue_mock:
            enqueue_mock.return_value = rules_index_jobs.EnqueueRulesIndexJobResult(
                enqueued=True,
                skipped_dedupe=False,
                message_id="1-0",
            )
            with patch(
                "dermascan.rules_engine.engine.rules_engine.register_index_batch_pending",
            ) as batch_mock:
                results = rules_engine.ensure_reports_exists(
                    ["Niacinamide"],
                    index_schedule=schedule,
                )

        assert len(results) == 1
        assert results[0].indexed is False
        assert results[0].source == "queued"
        enqueue_mock.assert_called_once()
        batch_mock.assert_called_once()

    def test_async_without_schedule_fails_closed(
        self,
        rules_engine: RulesEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Async mode without schedule does not call vector store."""
        monkeypatch.setenv("RULES_INDEX_INLINE", "false")
        row = {"ingredient_id": "ing-1", "download_url": "https://example.com/a.pdf"}
        rules_engine._fetch_report_rows = MagicMock(return_value={"niacinamide": row})  # type: ignore[method-assign]
        rules_engine._is_cached = MagicMock(return_value=False)  # type: ignore[method-assign]

        results = rules_engine.ensure_reports_exists(["Niacinamide"])

        assert results[0].source == "queue_failed"
        rules_engine.vector_store.index_pdf_from_firebase.assert_not_called()


@pytest.mark.unit
class TestEnsureReportsExistsInline:
    """Legacy synchronous indexing."""

    def test_inline_indexes_pdf(
        self,
        rules_engine: RulesEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RULES_INDEX_INLINE=true still indexes synchronously."""
        monkeypatch.setenv("RULES_INDEX_INLINE", "true")
        row = {
            "ingredient_id": "ing-1",
            "download_url": "https://example.com/a.pdf",
            "cir_ingredient_name": "Niacinamide",
            "function": "active",
            "report_url": "",
            "attachment_url": "",
            "storage_path": "",
        }
        rules_engine._fetch_report_rows = MagicMock(return_value={"niacinamide": row})  # type: ignore[method-assign]
        rules_engine._is_cached = MagicMock(return_value=False)  # type: ignore[method-assign]
        rules_engine._mark_cached = MagicMock()  # type: ignore[method-assign]

        results = rules_engine.ensure_reports_exists(["Niacinamide"])

        assert results[0].indexed is True
        assert results[0].source == "indexed_now"
        rules_engine.vector_store.index_pdf_from_firebase.assert_called_once()

    def test_queue_dedupe_skip_uses_cache_when_available(
        self,
        rules_engine: RulesEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deduped enqueue returns cache hit when indexing completed elsewhere."""
        monkeypatch.setenv("RULES_INDEX_INLINE", "false")
        row = {
            "ingredient_id": "ing-1",
            "download_url": "https://example.com/a.pdf",
            "cir_ingredient_name": "Niacinamide",
            "function": "active",
            "report_url": "",
            "attachment_url": "",
            "storage_path": "",
        }
        rules_engine._fetch_report_rows = MagicMock(return_value={"niacinamide": row})  # type: ignore[method-assign]
        rules_engine._is_cached = MagicMock(side_effect=[False, True])  # type: ignore[method-assign]
        rules_engine._read_cached_entry = MagicMock(  # type: ignore[method-assign]
            return_value={"ingredient_id": "ing-1", "indexed": "true"},
        )
        schedule = rules_index_jobs.RulesIndexScheduleContext(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
        )
        with patch(
            "dermascan.rules_engine.engine.rules_engine.enqueue_rules_index_job",
        ) as enqueue_mock:
            enqueue_mock.return_value = rules_index_jobs.EnqueueRulesIndexJobResult(
                enqueued=False,
                skipped_dedupe=True,
            )
            with patch(
                "dermascan.rules_engine.engine.rules_engine.register_index_batch_pending",
            ) as batch_mock:
                results = rules_engine.ensure_reports_exists(
                    ["Niacinamide"],
                    index_schedule=schedule,
                )
        assert results[0].indexed is True
        assert results[0].source == "redis_cache"
        batch_mock.assert_not_called()

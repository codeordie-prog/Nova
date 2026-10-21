"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for rules index worker job processing."""


from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

_vector_store_stub = ModuleType("dermascan.rules_engine.vector_store.build_vectorstore")
_vector_store_stub.VectorStore = MagicMock
sys.modules["dermascan.rules_engine.vector_store.build_vectorstore"] = _vector_store_stub

from dermascan.backend.services.agents import indexing_worker  # noqa: E402
from dermascan.backend.services.agents import rules_index_jobs


@pytest.mark.unit
class TestProcessIndexJob:
    """Index worker handler behavior."""

    def test_process_success_marks_cache_and_acks(self) -> None:
        """Successful index clears dedupe, finalizes batch, and acks."""
        job = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="c-1",
            message_id="m-1",
            channel="whatsapp",
            ingredient="Niacinamide",
            normalized_name="niacinamide",
            ingredient_id="ing-1",
            pdf_url="https://example.com/n.pdf",
            cir_ingredient_name="Niacinamide",
        )
        fields = rules_index_jobs.stream_fields_from_job(job)
        stop_event = MagicMock()

        with patch.object(indexing_worker, "parse_job_fields", return_value=job):
            with patch.object(indexing_worker, "start_stream_heartbeat", return_value=stop_event):
                with patch.object(indexing_worker, "_run_index_job") as run_mock:
                    with patch.object(indexing_worker, "clear_rules_index_job_dedupe") as dedupe_mock:
                        with patch.object(indexing_worker, "finalize_index_batch_if_complete") as fin_mock:
                            with patch.object(indexing_worker, "_ack_and_delete") as ack_mock:
                                with patch.object(indexing_worker.reclaim_logic, "mark_processed"):
                                    indexing_worker._process_index_job("1-0", fields)

        run_mock.assert_called_once()
        dedupe_mock.assert_called_once()
        fin_mock.assert_called_once()
        assert fin_mock.call_args.kwargs.get("job_succeeded") is True
        ack_mock.assert_called_once_with("1-0")
        stop_event.set.assert_called_once()

    def test_process_failure_writes_dlq(self) -> None:
        """Failures still finalize batch and ack after DLQ."""
        job = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="c-1",
            message_id="m-1",
            channel="whatsapp",
            ingredient="Niacinamide",
            normalized_name="niacinamide",
            ingredient_id="ing-1",
            pdf_url="https://example.com/n.pdf",
            cir_ingredient_name="Niacinamide",
        )
        stop_event = MagicMock()

        with patch.object(indexing_worker, "parse_job_fields", return_value=job):
            with patch.object(indexing_worker, "start_stream_heartbeat", return_value=stop_event):
                with patch.object(indexing_worker, "_run_index_job", side_effect=RuntimeError("pdf fail")):
                    with patch.object(indexing_worker, "_write_dlq") as dlq_mock:
                        with patch.object(indexing_worker, "clear_rules_index_job_dedupe"):
                            with patch.object(indexing_worker, "finalize_index_batch_if_complete") as fin_mock:
                                with patch.object(indexing_worker, "_ack_and_delete"):
                                    with patch.object(indexing_worker, "_emit_index_event"):
                                        with patch.object(indexing_worker.reclaim_logic, "mark_processed"):
                                            indexing_worker._process_index_job("2-0", {})

        dlq_mock.assert_called_once()
        assert fin_mock.call_args.kwargs.get("job_succeeded") is False

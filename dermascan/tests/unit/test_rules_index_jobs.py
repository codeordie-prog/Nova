"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for async rules index job enqueue helpers."""


from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dermascan.backend.services.agents import rules_index_jobs


@pytest.fixture(autouse=True)
def _redis_url_for_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_with_redis_retries requires REDIS_URL even when Redis is mocked."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.mark.unit
class TestRulesIndexJobPayload:
    """Serialization and parsing of index job payloads."""

    def test_stream_fields_round_trip(self) -> None:
        """Stream fields encode and decode to the same payload."""
        payload = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
            ingredient="Ascorbic Acid",
            normalized_name="ascorbic acid",
            ingredient_id="ing-1",
            pdf_url="https://example.com/a.pdf",
            cir_ingredient_name="Ascorbic Acid",
            function="antioxidant",
        )
        fields = rules_index_jobs.stream_fields_from_job(payload)
        parsed = rules_index_jobs.parse_job_fields(fields)

        assert parsed == payload
        assert json.loads(fields["payload"])["pdf_url"] == "https://example.com/a.pdf"

    def test_parse_job_fields_rejects_empty_payload(self) -> None:
        """Missing payload requires top-level required fields."""
        with pytest.raises(Exception):
            rules_index_jobs.parse_job_fields({"request_id": "only"})


@pytest.mark.unit
class TestEnqueueRulesIndexJob:
    """Dedupe and XADD behavior."""

    def test_enqueue_sets_dedupe_and_xadds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful enqueue acquires dedupe and adds a stream message."""
        redis_client = MagicMock()
        redis_client.set.return_value = True
        redis_client.xadd.return_value = "1700000000000-0"

        payload = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
            ingredient="Niacinamide",
            normalized_name="niacinamide",
            ingredient_id="ing-2",
            pdf_url="https://example.com/n.pdf",
            cir_ingredient_name="Niacinamide",
        )

        result = rules_index_jobs.enqueue_rules_index_job(redis_client, payload)

        assert result.enqueued is True
        assert result.skipped_dedupe is False
        assert result.message_id == "1700000000000-0"
        redis_client.set.assert_called_once()
        _args, kwargs = redis_client.set.call_args
        assert kwargs.get("nx") is True
        assert "ex" in kwargs
        redis_client.xadd.assert_called_once()

    def test_enqueue_skips_when_dedupe_locked(self) -> None:
        """When SETNX fails, no stream message is added."""
        redis_client = MagicMock()
        redis_client.set.return_value = False

        payload = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
            ingredient="Niacinamide",
            normalized_name="niacinamide",
            ingredient_id="ing-2",
            pdf_url="https://example.com/n.pdf",
            cir_ingredient_name="Niacinamide",
        )

        result = rules_index_jobs.enqueue_rules_index_job(redis_client, payload)

        assert result.enqueued is False
        assert result.skipped_dedupe is True
        redis_client.xadd.assert_not_called()


@pytest.mark.unit
class TestBuildIndexJobPayload:
    """Payload builder from Postgres rows."""

    def test_build_index_job_payload_maps_row(self) -> None:
        """Report row fields map into the job payload."""
        schedule = rules_index_jobs.RulesIndexScheduleContext(
            request_id="req-1",
            case_id="case-1",
            client_id="client-1",
            message_id="msg-1",
            channel="whatsapp",
        )
        row = {
            "ingredient_id": "uuid-1",
            "download_url": "https://storage/a.pdf",
            "cir_ingredient_name": "Ascorbic Acid",
            "function": "antioxidant",
            "report_url": "https://cir/r",
            "attachment_url": "https://att/a",
            "storage_path": "cir-pdfs/a.pdf",
        }
        job = rules_index_jobs.build_index_job_payload(
            schedule,
            ingredient="Ascorbic Acid",
            normalized_name="ascorbicacid",
            row=row,
        )
        assert job.ingredient_id == "uuid-1"
        assert job.pdf_url == "https://storage/a.pdf"
        assert job.storage_path == "cir-pdfs/a.pdf"


@pytest.mark.unit
class TestRegisterIndexBatch:
    """Batch SET coordination."""

    def test_register_index_batch_pending_sadd_and_expire(self) -> None:
        """Pending ingredients are tracked on a TTL-backed SET."""
        redis_client = MagicMock()
        rules_index_jobs.register_index_batch_pending(
            redis_client,
            "req-99",
            "niacinamide",
        )
        redis_client.sadd.assert_called_once()
        redis_client.expire.assert_called_once()


@pytest.mark.unit
class TestFinalizeIndexBatch:
    """Batch completion and index-ready signal."""

    def test_finalize_no_signal_while_pending(self) -> None:
        """Signal is not emitted until the batch SET is empty."""
        redis_client = MagicMock()
        job = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-1",
            case_id="case-1",
            client_id="c-1",
            message_id="m-1",
            channel="whatsapp",
            ingredient="A",
            normalized_name="a",
            ingredient_id="id-1",
            pdf_url="https://example.com/a.pdf",
            cir_ingredient_name="A",
        )
        with patch.object(
            rules_index_jobs,
            "complete_index_batch_member",
            return_value=2,
        ):
            with patch.object(rules_index_jobs, "try_enqueue_product_review_index_ready") as ready_mock:
                emitted = rules_index_jobs.finalize_index_batch_if_complete(
                    redis_client,
                    job,
                    job_succeeded=True,
                )
        assert emitted is False
        ready_mock.assert_not_called()

    def test_finalize_emits_success_when_batch_done(self) -> None:
        """Empty batch with no failures emits success outcome."""
        redis_client = MagicMock()
        redis_client.scard.return_value = 0
        job = rules_index_jobs.RulesIndexJobPayload(
            request_id="req-2",
            case_id="case-2",
            client_id="c-2",
            message_id="m-2",
            channel="whatsapp",
            ingredient="B",
            normalized_name="b",
            ingredient_id="id-2",
            pdf_url="https://example.com/b.pdf",
            cir_ingredient_name="B",
        )
        with patch.object(rules_index_jobs, "complete_index_batch_member", return_value=0):
            with patch.object(
                rules_index_jobs,
                "try_enqueue_product_review_index_ready",
                return_value=True,
            ) as ready_mock:
                emitted = rules_index_jobs.finalize_index_batch_if_complete(
                    redis_client,
                    job,
                    job_succeeded=True,
                )
        assert emitted is True
        ready_mock.assert_called_once()
        _args, kwargs = ready_mock.call_args
        assert kwargs.get("index_outcome") == "success"


@pytest.mark.unit
class TestEnsureConsumerGroup:
    """Consumer group bootstrap."""

    def test_busygroup_is_ignored(self) -> None:
        """Existing consumer group does not raise."""
        redis_client = MagicMock()
        redis_client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")

        rules_index_jobs.ensure_rules_index_consumer_group(redis_client)

        redis_client.xgroup_create.assert_called_once()

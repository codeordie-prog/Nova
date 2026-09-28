"""Integration-style async CIR index flow (fakeredis, no PDF/torch)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dermascan.backend.services.agents import rules_index_jobs


@pytest.fixture(autouse=True)
def _redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")


def _job(
    *,
    request_id: str = "req-flow-1",
    normalized_name: str,
    ingredient: str | None = None,
) -> rules_index_jobs.RulesIndexJobPayload:
    """Build a minimal index job for flow tests."""
    display = ingredient or normalized_name.title()
    return rules_index_jobs.RulesIndexJobPayload(
        request_id=request_id,
        case_id="case-flow-1",
        client_id="client-flow-1",
        message_id="msg-flow-1",
        channel="whatsapp",
        ingredient=display,
        normalized_name=normalized_name,
        ingredient_id=f"id-{normalized_name}",
        pdf_url=f"https://example.com/{normalized_name}.pdf",
        cir_ingredient_name=display,
    )


def _orchestrator_signals(fake_redis: Any) -> list[dict[str, str]]:
    """Read all entries on the orchestrator signal stream."""
    entries = fake_redis.xrange(rules_index_jobs.ORCHESTRATOR_SIGNAL_STREAM, "-", "+")
    return [fields for _msg_id, fields in entries]


@pytest.mark.unit
class TestAsyncIndexFlowFakeredis:
    """Queue → worker batch completion → index-ready signal."""

    def test_enqueue_batch_and_index_ready_signal(self, fake_redis: Any) -> None:
        """Two ingredients: first completion keeps batch open; second emits signal."""
        request_id = "req-flow-1"
        job_a = _job(normalized_name="niacinamide")
        job_b = _job(normalized_name="retinol")

        rules_index_jobs.register_index_batch_pending(fake_redis, request_id, "niacinamide")
        rules_index_jobs.register_index_batch_pending(fake_redis, request_id, "retinol")

        enq_a = rules_index_jobs.enqueue_rules_index_job(fake_redis, job_a)
        enq_b = rules_index_jobs.enqueue_rules_index_job(fake_redis, job_b)
        assert enq_a.enqueued and enq_b.enqueued

        stream_len = fake_redis.xlen(rules_index_jobs.RULES_INDEX_STREAM)
        assert stream_len == 2

        rules_index_jobs.save_product_index_context(
            fake_redis,
            request_id,
            {"product_name": "Test Serum", "request_id": request_id},
        )
        ctx_raw = fake_redis.get(rules_index_jobs.rules_index_context_key(request_id))
        assert ctx_raw is not None
        assert json.loads(ctx_raw)["product_name"] == "Test Serum"

        assert rules_index_jobs.claim_product_index_pending_sent(fake_redis, request_id) is True
        assert rules_index_jobs.claim_product_index_pending_sent(fake_redis, request_id) is False

        emitted_first = rules_index_jobs.finalize_index_batch_if_complete(
            fake_redis,
            job_a,
            job_succeeded=True,
        )
        assert emitted_first is False
        assert _orchestrator_signals(fake_redis) == []

        emitted_second = rules_index_jobs.finalize_index_batch_if_complete(
            fake_redis,
            job_b,
            job_succeeded=True,
        )
        assert emitted_second is True

        signals = _orchestrator_signals(fake_redis)
        assert len(signals) == 1
        fields = signals[0]
        assert fields["signal_type"] == rules_index_jobs.ORCHESTRATOR_SIGNAL_TYPE_INDEX_READY
        assert fields["request_id"] == request_id
        assert fields["channel"] == "whatsapp"
        payload = json.loads(fields["payload"])
        assert payload["index_outcome"] == "success"

        job_c = _job(normalized_name="vitamin_c")
        assert (
            rules_index_jobs.try_enqueue_product_review_index_ready(
                fake_redis,
                job_c,
                index_outcome="success",
            )
            is False
        )
        assert len(_orchestrator_signals(fake_redis)) == 1

    def test_partial_batch_outcome_when_one_job_fails(self, fake_redis: Any) -> None:
        """Failed member yields partial index_outcome on the ready signal."""
        request_id = "req-flow-partial"
        job_ok = _job(request_id=request_id, normalized_name="glycerin")
        job_bad = _job(request_id=request_id, normalized_name="unknown_x")

        rules_index_jobs.register_index_batch_pending(fake_redis, request_id, "glycerin")
        rules_index_jobs.register_index_batch_pending(fake_redis, request_id, "unknown_x")

        rules_index_jobs.finalize_index_batch_if_complete(fake_redis, job_ok, job_succeeded=True)
        rules_index_jobs.finalize_index_batch_if_complete(fake_redis, job_bad, job_succeeded=False)

        signals = _orchestrator_signals(fake_redis)
        assert len(signals) == 1
        payload = json.loads(signals[0]["payload"])
        assert payload["index_outcome"] == "partial"

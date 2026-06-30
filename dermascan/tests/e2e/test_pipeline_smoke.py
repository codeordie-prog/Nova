"""Live-stack smoke tests for the full ingress → worker → outbound pipeline.

Requires:
- Redis with REDIS_URL
- Backend API on DERMASCAN_API_URL (default http://localhost:8088)
- ``poetry run python dermascan/scripts/do_work.py`` (orchestrator + outbound workers)
- Qwen/OpenAI/Gemini for orchestrator LLM calls
- MedGemma gateway + worker for image diagnosis jobs
"""

from __future__ import annotations

import uuid

import pytest

from dermascan.tests.e2e.stack_helpers import (
    assert_webhook_queued,
    enqueue_image_job,
    enqueue_text_job,
    record_e2e_result,
    wait_for_orchestrator_reply,
    wait_for_pipeline_completion,
)


@pytest.mark.e2e
@pytest.mark.slow
class TestPipelineSmoke:
    """Single-job end-to-end completion against a live stack."""

    def test_text_job_completes(self, e2e_stack, e2e_redis) -> None:
        """Text webhook → orchestrator worker → non-empty outbound reply."""
        result = enqueue_text_job(e2e_stack)
        request_id = assert_webhook_queued(result)

        reply = wait_for_orchestrator_reply(e2e_redis, e2e_stack, request_id)
        assert reply.reply_text
        print(
            f"text E2E completed in {reply.elapsed_s:.1f}s "
            f"kind={reply.kind!r} reply_len={len(reply.reply_text)}"
        )
        record_e2e_result(
            suite="smoke",
            test_name="test_text_job_completes",
            latencies_s=[reply.elapsed_s],
            concurrency=1,
            success_count=1,
            job_count=1,
            sample_reply=reply.reply_text,
        )

    @pytest.mark.skip(reason="Image E2E disabled — text-only runs for now")
    def test_image_job_completes(
        self,
        e2e_stack,
        e2e_redis,
        sample_image_bytes: list[bytes],
    ) -> None:
        """Image upload + webhook → diagnosis path completes."""
        result = enqueue_image_job(e2e_stack, sample_image_bytes[0])
        request_id = assert_webhook_queued(result)

        completion = wait_for_pipeline_completion(e2e_redis, e2e_stack, request_id)
        assert completion.marked_processed or completion.saw_outbound
        print(
            f"image E2E completed in {completion.elapsed_s:.1f}s "
            f"(processed={completion.marked_processed}, outbound={completion.saw_outbound})"
        )

    def test_duplicate_webhook_replay_completes_without_5xx(
        self,
        e2e_stack,
        e2e_redis,
    ) -> None:
        """Replaying the same provider message_id enqueues twice today; both should complete.

        Ingress-level dedup (R10) is not implemented yet — this test documents current
        behavior and ensures replays do not crash the pipeline.
        """
        message_id = f"wamid.e2e.dup.{uuid.uuid4().hex}"
        from_id = f"2547{uuid.uuid4().int % 10_000_000_000:010d}"

        first = enqueue_text_job(e2e_stack, message_id=message_id, from_id=from_id)
        second = enqueue_text_job(e2e_stack, message_id=message_id, from_id=from_id)

        first_id = assert_webhook_queued(first)
        second_id = assert_webhook_queued(second)
        assert first_id != second_id, "Each ingress call should receive a unique request_id"

        first_done = wait_for_pipeline_completion(e2e_redis, e2e_stack, first_id)
        second_done = wait_for_pipeline_completion(e2e_redis, e2e_stack, second_id)

        assert first_done.marked_processed or first_done.saw_outbound
        assert second_done.marked_processed or second_done.saw_outbound

        record_e2e_result(
            suite="smoke",
            test_name="test_duplicate_webhook_replay_completes_without_5xx",
            latencies_s=[first_done.elapsed_s, second_done.elapsed_s],
            concurrency=1,
            success_count=2,
            job_count=2,
            notes="duplicate message_id; ingress dedup not implemented",
        )
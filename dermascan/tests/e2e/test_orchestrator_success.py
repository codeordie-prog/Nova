"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Live-stack orchestrator success probes (text-only, no images).

Validates that parallel text jobs produce non-empty outbound replies from the
orchestrator at configured concurrency levels (default 20 and 40).
"""


from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from dermascan.tests.e2e.stack_helpers import (
    E2EConfig,
    OrchestratorReply,
    assert_webhook_queued,
    enqueue_text_job,
    load_e2e_concurrency_levels,
    record_e2e_result,
    summarize_latencies_ms,
    wait_for_orchestrator_reply,
)


def _run_parallel_orchestrator_jobs(
    cfg: E2EConfig,
    redis_client,
    count: int,
) -> list[OrchestratorReply]:
    """Enqueue ``count`` parallel text jobs and wait for orchestrator replies."""

    def _one_job(index: int) -> OrchestratorReply:
        result = enqueue_text_job(
            cfg,
            message_id=f"wamid.e2e.orch.{uuid.uuid4().hex}",
            from_id=f"2547{(uuid.uuid4().int + index) % 10_000_000_000:010d}",
            text=f"E2E orchestrator success probe {index} — reply in one short sentence.",
        )
        request_id = assert_webhook_queued(result)
        reply = wait_for_orchestrator_reply(redis_client, cfg, request_id)
        assert reply.reply_text
        return reply

    replies: list[OrchestratorReply] = []
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(_one_job, index) for index in range(count)]
        for future in as_completed(futures):
            replies.append(future.result())
    return replies


@pytest.mark.e2e
@pytest.mark.slow
class TestOrchestratorSuccess:
    """Orchestrator produces outbound replies under parallel text load."""

    @pytest.mark.parametrize("concurrency", load_e2e_concurrency_levels())
    def test_parallel_text_jobs_return_outbound_replies(
        self,
        e2e_stack: E2EConfig,
        e2e_redis,
        concurrency: int,
    ) -> None:
        """At each level, every text job gets a non-empty orchestrator reply."""
        replies = _run_parallel_orchestrator_jobs(e2e_stack, e2e_redis, concurrency)
        assert len(replies) == concurrency

        latencies_s = [reply.elapsed_s for reply in replies]
        summary = summarize_latencies_ms(latencies_s)
        sample = replies[0].reply_text[:80].replace("\n", " ")

        print(
            f"orchestrator concurrency={concurrency} "
            f"success={len(replies)}/{concurrency} "
            f"p50={summary['p50_ms']:.0f}ms "
            f"p95={summary['p95_ms']:.0f}ms "
            f"p99={summary['p99_ms']:.0f}ms "
            f"sample_reply={sample!r}..."
        )
        record_e2e_result(
            suite="orchestrator",
            test_name=f"test_parallel_text_jobs_return_outbound_replies[{concurrency}]",
            latencies_s=latencies_s,
            concurrency=concurrency,
            success_count=len(replies),
            job_count=concurrency,
            sample_reply=replies[0].reply_text,
            notes="parallel text-only orchestrator replies",
        )

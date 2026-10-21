"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Live-stack E2E concurrency probes (parallel unique text jobs).

Uses unique provider message ids per worker thread. Image jobs disabled for now.
Orchestrator success at scale is covered in ``test_orchestrator_success.py``.
"""


from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from dermascan.tests.e2e.stack_helpers import (
    E2EConfig,
    assert_webhook_queued,
    enqueue_text_job,
    record_e2e_result,
    summarize_latencies_ms,
    wait_for_orchestrator_reply,
)


def _parallel_jobs(default: int) -> int:
    raw = os.getenv("DERMASCAN_E2E_PARALLEL_JOBS", str(default)).strip()
    return max(1, int(raw))


def _run_parallel_text_jobs(cfg: E2EConfig, redis_client, count: int) -> list[float]:
    """Enqueue and wait for ``count`` parallel text jobs; return completion times."""

    def _one_job(index: int) -> float:
        result = enqueue_text_job(
            cfg,
            message_id=f"wamid.e2e.par.{uuid.uuid4().hex}",
            from_id=f"2547{(uuid.uuid4().int + index) % 10_000_000_000:010d}",
            text=f"E2E parallel text job {index}",
        )
        request_id = assert_webhook_queued(result)
        reply = wait_for_orchestrator_reply(redis_client, cfg, request_id)
        assert reply.reply_text
        return reply.elapsed_s

    latencies: list[float] = []
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(_one_job, index) for index in range(count)]
        for future in as_completed(futures):
            latencies.append(future.result())
    return latencies


@pytest.mark.e2e
@pytest.mark.slow
class TestPipelineConcurrency:
    """Parallel unique text jobs against a live worker stack (lightweight)."""

    def test_parallel_text_jobs_complete(self, e2e_stack, e2e_redis) -> None:
        """N concurrent text jobs all return orchestrator outbound replies."""
        count = _parallel_jobs(5)
        latencies = _run_parallel_text_jobs(e2e_stack, e2e_redis, count)
        assert len(latencies) == count
        summary = summarize_latencies_ms(latencies)
        print(
            f"parallel text jobs={count} "
            f"p50={summary['p50_ms']:.0f}ms p95={summary['p95_ms']:.0f}ms "
            f"max={max(latencies):.1f}s min={min(latencies):.1f}s"
        )
        record_e2e_result(
            suite="concurrency",
            test_name="test_parallel_text_jobs_complete",
            latencies_s=latencies,
            concurrency=count,
            success_count=len(latencies),
            job_count=count,
            notes="lightweight parallel text probe",
        )

    @pytest.mark.skip(reason="Image E2E disabled — text-only runs for now")
    def test_parallel_image_jobs_complete(
        self,
        e2e_stack,
        e2e_redis,
        sample_image_bytes: list[bytes],
    ) -> None:
        """N concurrent image jobs complete (keep N small — inference bound)."""
        pass

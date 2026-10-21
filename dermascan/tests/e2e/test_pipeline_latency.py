"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Live-stack E2E latency probes (sequential jobs).

Measures pipeline time from webhook acceptance to worker completion.
Separate from ingress-only p95 probes in ``tests/API/test_ingress_p95.py``.
"""


from __future__ import annotations

import os

import pytest

from dermascan.tests.e2e.stack_helpers import (
    assert_webhook_queued,
    enqueue_image_job,
    enqueue_text_job,
    record_e2e_result,
    summarize_latencies_ms,
    wait_for_orchestrator_reply,
    wait_for_pipeline_completion,
)


def _pipeline_rounds(default: int) -> int:
    raw = os.getenv("DERMASCAN_E2E_PIPELINE_ROUNDS", str(default)).strip()
    return max(1, int(raw))


@pytest.mark.e2e
@pytest.mark.slow
class TestPipelineLatency:
    """Sequential E2E latency percentiles over a live stack."""

    def test_text_pipeline_latency_percentiles(self, e2e_stack, e2e_redis) -> None:
        """Record p50/p95/p99 for text-only orchestrator replies."""
        rounds = _pipeline_rounds(3)
        latencies_s: list[float] = []

        for index in range(rounds):
            result = enqueue_text_job(e2e_stack, text=f"E2E latency text probe {index}")
            request_id = assert_webhook_queued(result)
            reply = wait_for_orchestrator_reply(e2e_redis, e2e_stack, request_id)
            latencies_s.append(reply.elapsed_s)

        summary = summarize_latencies_ms(latencies_s)
        print(
            f"text orchestrator latency over {rounds} jobs: "
            f"p50={summary['p50_ms']:.0f}ms "
            f"p95={summary['p95_ms']:.0f}ms "
            f"p99={summary['p99_ms']:.0f}ms"
        )
        assert summary["p95_ms"] > 0
        record_e2e_result(
            suite="latency",
            test_name="test_text_pipeline_latency_percentiles",
            latencies_s=latencies_s,
            concurrency=1,
            success_count=rounds,
            job_count=rounds,
            notes=f"sequential rounds={rounds}",
        )

    @pytest.mark.skip(reason="Image E2E disabled — text-only runs for now")
    def test_image_pipeline_latency_percentiles(
        self,
        e2e_stack,
        e2e_redis,
        sample_image_bytes: list[bytes],
    ) -> None:
        """Record p50/p95/p99 for image diagnosis jobs (includes MedGemma)."""
        rounds = _pipeline_rounds(2)
        latencies_s: list[float] = []

        for index in range(rounds):
            image = sample_image_bytes[index % len(sample_image_bytes)]
            result = enqueue_image_job(
                e2e_stack,
                image,
                text=f"E2E latency image probe {index}",
            )
            request_id = assert_webhook_queued(result)
            completion = wait_for_pipeline_completion(e2e_redis, e2e_stack, request_id)
            latencies_s.append(completion.elapsed_s)

        summary = summarize_latencies_ms(latencies_s)
        print(
            f"image pipeline latency over {rounds} jobs: "
            f"p50={summary['p50_ms']:.0f}ms "
            f"p95={summary['p95_ms']:.0f}ms "
            f"p99={summary['p99_ms']:.0f}ms"
        )
        assert summary["p95_ms"] > 0

        slo_ms = float(os.getenv("DERMASCAN_E2E_IMAGE_P95_SLO_MS", "0"))
        if slo_ms > 0:
            assert summary["p95_ms"] <= slo_ms, (
                f"Image pipeline p95 {summary['p95_ms']:.0f}ms exceeds SLO {slo_ms:.0f}ms"
            )

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Integration tests: admission gates end-to-end at ingress.

Concerns: S1 — admission caps input to drain rate; R9 — malformed ingress;
          R10 — idempotent webhook re-delivery.
Requires real Redis (testcontainers).
"""


from __future__ import annotations

import pytest


@pytest.mark.integration
class TestAdmissionGates:
    """Rate-limit 429 and queue red 429 at FastAPI ingress."""

    def test_over_rpm_returns_429_with_retry_after(self, real_redis, app_client) -> None:
        """S1: Sustained RPS > RPM → 429 with Retry-After; never over-admitted."""
        pytest.skip("Scaffold: implementation pending")

    def test_queue_red_returns_429_immediately(self, real_redis, app_client) -> None:
        """S1/S3: XPENDING at hard limit → immediate 429 (no unbounded queue)."""
        pytest.skip("Scaffold: implementation pending")

    def test_bad_internal_secret_returns_401(self, app_client) -> None:
        """R9: Invalid X-Internal-Secret → 401; job never enqueued."""
        pytest.skip("Scaffold: implementation pending")

    def test_malformed_payload_returns_422(self, app_client) -> None:
        """R9: Schema validation failure → 422; job never enqueued."""
        pytest.skip("Scaffold: implementation pending")

    def test_duplicate_webhook_single_enqueue(self, real_redis, app_client) -> None:
        """R10: Same provider message twice → single stream entry."""
        pytest.skip("Scaffold: implementation pending")

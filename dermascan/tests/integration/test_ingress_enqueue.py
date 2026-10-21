"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Integration tests: webhook ingress → Redis stream enqueue.

Requires real Redis (testcontainers). Verifies stream entry shape and ack response.
Source: dermascan/backend/api/endpoints.py
"""


from __future__ import annotations

import pytest


@pytest.mark.integration
class TestIngressEnqueue:
    """Webhook POST → orchestrator:jobs stream entry lifecycle."""

    def test_whatsapp_webhook_enqueues_stream_entry(self, real_redis, app_client) -> None:
        """Valid WhatsApp webhook → XADD with expected fields; 200/202 ack."""
        pytest.skip("Scaffold: implementation pending")

    def test_telegram_webhook_enqueues_stream_entry(self, real_redis, app_client) -> None:
        """Valid Telegram webhook → XADD with expected fields; 200/202 ack."""
        pytest.skip("Scaffold: implementation pending")

    def test_orchestrator_request_enqueues_stream_entry(self, real_redis, app_client) -> None:
        """POST /v1/orchestrator/requests → stream entry with request metadata."""
        pytest.skip("Scaffold: implementation pending")

    def test_stream_entry_shape_matches_schema(self, real_redis, app_client) -> None:
        """Enqueued fields match expected job envelope schema."""
        pytest.skip("Scaffold: implementation pending")

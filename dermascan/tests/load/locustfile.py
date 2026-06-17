"""Locust load-test entry point for Dermascan ingress.

Run against a live stack:
    locust -f dermascan/tests/load/locustfile.py --host http://localhost:8080

Profiles and pass criteria: see profiles.md.
Concerns: L1 — max sustainable RPS; S5 — throughput vs concurrency curve.
"""

from __future__ import annotations

import os

# Locust is an optional test dependency — import guarded for scaffold collection.
try:
    from locust import HttpUser, between, task
except ImportError:
    HttpUser = object  # type: ignore[misc, assignment]
    between = task = lambda *a, **k: lambda f: f  # type: ignore[misc]


_INTERNAL_SECRET = os.getenv("DERMASCAN_INTERNAL_SECRET", "<secret>")


class IngressUser(HttpUser):
    """Sustained ingress load against WhatsApp webhook."""

    wait_time = between(0.1, 0.5)

    @task
    def whatsapp_webhook(self) -> None:
        """POST /v1/webhooks/whatsapp with a valid envelope scaffold."""
        self.client.post(
            "/v1/webhooks/whatsapp",
            headers={"X-Internal-Secret": _INTERNAL_SECRET},
            json={
                "client_id": "load-test-client",
                "message_id": "load-test-msg",
                "text": "Load test message",
            },
        )

    @task(2)
    def health_check(self) -> None:
        """GET /health — baseline availability probe."""
        self.client.get("/health")

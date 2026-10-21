"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Locust load-test entry point for Dermascan backend ingress.

Run against a live stack:
    locust -f dermascan/tests/load/locustfile.py --host http://localhost:8088

Profiles and pass criteria: see profiles.md.
Concerns: L1 — max sustainable RPS; S5 — throughput vs concurrency curve.
"""


from __future__ import annotations

import os
import uuid

try:
    from locust import HttpUser, between, task
except ImportError:
    HttpUser = object  # type: ignore[misc, assignment]
    between = task = lambda *a, **k: lambda f: f  # type: ignore[misc]

_INTERNAL_SECRET = os.getenv(
    "WHATSAPP_INGRESS_SECRET",
    os.getenv("INTERNAL_API_SECRET", os.getenv("DERMASCAN_INTERNAL_SECRET", "")),
)


class IngressUser(HttpUser):
    """Sustained ingress load against WhatsApp webhook."""

    wait_time = between(0.1, 0.5)

    @task(3)
    def whatsapp_webhook(self) -> None:
        """POST /v1/webhooks/whatsapp with a valid text envelope."""
        if not _INTERNAL_SECRET:
            return

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "message": {
                                    "from": f"2547{uuid.uuid4().int % 10_000_000_000:010d}",
                                    "id": f"wamid.load.{uuid.uuid4().hex}",
                                    "type": "text",
                                    "text": {"body": "Locust ingress probe"},
                                }
                            }
                        }
                    ]
                }
            ]
        }
        self.client.post(
            "/v1/webhooks/whatsapp",
            headers={
                "X-Internal-Secret": _INTERNAL_SECRET,
                "Content-Type": "application/json",
            },
            json=payload,
            name="POST /v1/webhooks/whatsapp",
        )

    @task(1)
    def health_check(self) -> None:
        """GET /health — baseline availability probe."""
        self.client.get("/health", name="GET /health")

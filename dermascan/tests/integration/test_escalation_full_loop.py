"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

End-to-end escalation loop: producer → worker → webhook → orchestrator."""


from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dermascan.ai_service.postprocessing import escalation as escalation_module
from dermascan.backend.app.main import app
from dermascan.backend.api import endpoints as endpoints_module
from dermascan.backend.services.agents import escalation_worker as worker_module
from dermascan.backend.services.agents import orchestrator_worker as orchestrator_module
from dermascan.shared.hmac_auth import SIGNATURE_HEADER, TIMESTAMP_HEADER, signature_header


@dataclass
class _JobEntry:
    job_id: str = "job-1"
    prompt: str = "prompt"
    image_bytes: bytes = b""
    msg_id: str = "msg-1"
    request_id: str = "req-1"
    case_id: str = "req-1"
    client_id: str = "wa:123"
    message_id: str = "msg-user-1"
    channel: str = "whatsapp"
    group_id: str = ""
    group_expected_count: int = 0
    image_blob_path: str = "client-media/wa:123/photo.jpg"


LOW_CONFIDENCE_JSON = json.dumps(
    {
        "review_type": "skin review",
        "diagnosis": "Acne vulgaris, moderate",
        "confidence": 0.42,
        "summary": {"severity": "moderate"},
        "treatment": {"needs": {"barrier_repair": "yes"}},
    }
)


DERMA_NOTES = {
    "physician_id": "dr-smith",
    "reviewed_at_ms": 1718367000000,
    "disposition": "confirmed",
    "summary": "Agree with AI assessment. Moderate inflammatory acne.",
    "clinical_notes": "Recommend gradual retinoid introduction.",
    "follow_up": "Reassess in 6 weeks.",
}


class _StubDashboard:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> str:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                dashboard.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"caseId":"req-1"}')

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/api/internal/escalations/ingest"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def escalation_env(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: Any,
) -> dict[str, Any]:
    monkeypatch.setenv("QUTIS_INTERNAL_SECRET", "integration-test-secret")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("DIAGNOSE_CONFIDENCE_THRESHOLD", "0.75")
    monkeypatch.setattr(escalation_module, "_DIAGNOSE_CONFIDENCE_THRESHOLD", 0.75)

    monkeypatch.setattr(escalation_module, "_redis", fake_redis)
    monkeypatch.setattr(endpoints_module, "_redis", fake_redis)
    monkeypatch.setattr(worker_module, "_redis", fake_redis)
    worker_module.reclaim_logic.set_redis_client(fake_redis)

    stub = _StubDashboard()
    platform_url = stub.start()
    monkeypatch.setenv("ESCALATION_PLATFORM_URL", platform_url)
    monkeypatch.setattr(worker_module, "ESCALATION_PLATFORM_URL", platform_url)

    yield {"redis": fake_redis, "stub": stub, "platform_url": platform_url}
    stub.stop()


class TestEscalationFullLoop:
    def test_producer_emits_image_blob_path(self, escalation_env: dict[str, Any]) -> None:
        redis_client = escalation_env["redis"]
        entry = _JobEntry()
        escalated = escalation_module.route_case_after_inference(entry, LOW_CONFIDENCE_JSON, 0.42)
        assert escalated is True

        rows = redis_client.xrange(escalation_module._ESCALATION_STREAM)
        assert len(rows) == 1
        _, fields = rows[0]
        assert fields["image_blob_path"] == entry.image_blob_path

    def test_worker_relays_and_caches_payload(self, escalation_env: dict[str, Any]) -> None:
        redis_client = escalation_env["redis"]
        stub: _StubDashboard = escalation_env["stub"]
        entry = _JobEntry()
        escalation_module.route_case_after_inference(entry, LOW_CONFIDENCE_JSON, 0.42)

        job_id = redis_client.xrange(escalation_module._ESCALATION_STREAM)[0][0]
        fields = redis_client.xrange(escalation_module._ESCALATION_STREAM)[0][1]
        worker_module._process_escalation_job(job_id, fields)

        assert len(stub.requests) == 1
        relay = stub.requests[0]
        assert relay["headers"][SIGNATURE_HEADER].startswith("sha256=")
        assert relay["headers"][TIMESTAMP_HEADER]
        handoff = json.loads(relay["body"].decode("utf-8"))
        assert handoff["case"]["case_id"] == "req-1"

        cached = redis_client.get("escalation:signal_payload:req-1")
        assert cached is not None
        assert redis_client.get("escalation:image_path:req-1") == entry.image_blob_path

        # Fidelity: summary/treatment must survive the producer→worker hop into the
        # cached payload (deterministically, not via a racy signal-stream lookup).
        cached_payload = json.loads(cached)
        assert cached_payload["summary"]["severity"] == "moderate"
        assert cached_payload["treatment"]["needs"]["barrier_repair"] == "yes"
        assert cached_payload["client_id"] == entry.client_id

        events = [fields for _, fields in redis_client.xrange(worker_module.EVENTS_STREAM)]
        assert any(row.get("status") == "escalation_relay_completed" for row in events)

    def test_return_webhook_publishes_physician_reviewed_signal(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        redis_client = escalation_env["redis"]
        payload = {
            "mode": "diagnose",
            "request_id": "req-1",
            "case_id": "req-1",
            "client_id": "wa:123",
            "message_id": "msg-user-1",
            "channel": "whatsapp",
            "diagnosis": "Acne vulgaris, moderate",
            "escalation": True,
        }
        redis_client.set("escalation:signal_payload:req-1", json.dumps(payload))

        body = json.dumps({"case_id": "req-1", "derma_notes": DERMA_NOTES}).encode("utf-8")
        sig, ts = signature_header(body)
        client = TestClient(app)
        monkeypatch.setattr(endpoints_module, "_redis", redis_client)
        response = client.post(
            "/v1/webhooks/escalation",
            content=body,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sig,
                TIMESTAMP_HEADER: ts,
            },
        )

        assert response.status_code == 200
        signals = [fields for _, fields in redis_client.xrange(endpoints_module._ORCHESTRATOR_SIGNAL_STREAM)]
        reviewed = next(row for row in signals if row["signal_type"] == "diagnose_physician_reviewed")
        signal_payload = json.loads(reviewed["payload"])
        assert signal_payload["derma_notes"]["summary"] == DERMA_NOTES["summary"]
        assert signal_payload["diagnosis"] == "Acne vulgaris, moderate"
        assert redis_client.exists("escalation:closed:req-1")

    def test_image_proxy_410_after_close(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        redis_client = escalation_env["redis"]
        redis_client.set("escalation:image_path:req-1", "client-media/wa:123/photo.jpg")
        redis_client.set("escalation:closed:req-1", "1")

        sig, ts = signature_header(b"/v1/escalations/req-1/image")
        client = TestClient(app)
        monkeypatch.setattr(endpoints_module, "_redis", redis_client)
        response = client.get(
            "/v1/escalations/req-1/image",
            headers={SIGNATURE_HEADER: sig, TIMESTAMP_HEADER: ts},
        )
        assert response.status_code == 410

    def test_image_proxy_rejects_cross_case_replay(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A signature minted for req-1's path must not authorize req-2's image.
        redis_client = escalation_env["redis"]
        redis_client.set("escalation:image_path:req-2", "client-media/wa:123/photo.jpg")

        sig, ts = signature_header(b"/v1/escalations/req-1/image")
        client = TestClient(app)
        monkeypatch.setattr(endpoints_module, "_redis", redis_client)
        response = client.get(
            "/v1/escalations/req-2/image",
            headers={SIGNATURE_HEADER: sig, TIMESTAMP_HEADER: ts},
        )
        assert response.status_code == 401

    def test_orchestrator_reply_led_by_physician_notes(self) -> None:
        payload = {
            "mode": "diagnose",
            "diagnosis": "Acne vulgaris, moderate",
            "derma_notes": DERMA_NOTES,
        }
        reply = orchestrator_module._compose_physician_reviewed_reply(payload)
        assert DERMA_NOTES["summary"] in reply
        assert "Recommend gradual retinoid introduction." in reply

    def test_orchestrator_physician_reviewed_branch_enqueues_outbound(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        enqueue = MagicMock()
        monkeypatch.setattr(orchestrator_module, "_enqueue_outbound", enqueue)
        monkeypatch.setattr(orchestrator_module, "_set_inflight", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_del_inflight", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_append_proactive_log", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_redis", escalation_env["redis"])
        monkeypatch.setattr(orchestrator_module, "reclaim_logic", worker_module.reclaim_logic)

        fields = {
            "signal_type": "diagnose_physician_reviewed",
            "signal_id": "sig-1",
            "request_id": "req-1",
            "case_id": "req-1",
            "client_id": "wa:123",
            "message_id": "msg-user-1",
            "channel": "whatsapp",
            "payload": json.dumps({"mode": "diagnose", "derma_notes": DERMA_NOTES}),
        }
        orchestrator_module._process_signal_message(MagicMock(), "1-0", fields)
        enqueue.assert_called_once()
        assert DERMA_NOTES["summary"] in enqueue.call_args.kwargs["text"]

    def test_orchestrator_escalated_signal_uses_client_message_not_rules(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        enqueue = MagicMock()
        rules = MagicMock()
        monkeypatch.setattr(orchestrator_module, "_enqueue_outbound", enqueue)
        monkeypatch.setattr(orchestrator_module, "run_skin_signal_rules_flow", rules)
        monkeypatch.setattr(orchestrator_module, "_set_inflight", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_del_inflight", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_append_proactive_log", lambda *args, **kwargs: None)
        monkeypatch.setattr(orchestrator_module, "_redis", escalation_env["redis"])
        monkeypatch.setattr(orchestrator_module, "reclaim_logic", worker_module.reclaim_logic)

        entry = _JobEntry()
        escalation_module.route_case_after_inference(entry, LOW_CONFIDENCE_JSON, 0.42)
        signals = [fields for _, fields in escalation_env["redis"].xrange(escalation_module._ORCHESTRATOR_SIGNAL_STREAM)]
        escalated = next(row for row in signals if row["signal_type"] == "diagnose_escalated")

        orchestrator_module._process_signal_message(MagicMock(), "1-0", escalated)
        enqueue.assert_called_once()
        rules.assert_not_called()
        assert enqueue.call_args.kwargs["text"] == escalation_module.ESCALATION_CLIENT_MESSAGE
        payload = json.loads(escalated["payload"])
        assert "diagnosis" not in payload

    def test_relay_failure_enqueues_deferred_signal(
        self,
        escalation_env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        redis_client = escalation_env["redis"]
        monkeypatch.setattr(worker_module, "ESCALATION_PLATFORM_URL", "http://127.0.0.1:1/unreachable")
        monkeypatch.setattr(worker_module, "ESCALATION_RELAY_MAX_ATTEMPTS", 1)

        entry = _JobEntry()
        escalation_module.route_case_after_inference(entry, LOW_CONFIDENCE_JSON, 0.42)
        job_id, fields = redis_client.xrange(escalation_module._ESCALATION_STREAM)[0]
        worker_module._process_escalation_job(job_id, fields)

        dlq_rows = redis_client.xrange(worker_module.ESCALATION_DLQ_STREAM)
        assert len(dlq_rows) == 1
        signals = [fields for _, fields in redis_client.xrange(worker_module.ORCHESTRATOR_SIGNAL_STREAM)]
        assert any(row["signal_type"] == "diagnose_escalation_deferred" for row in signals)

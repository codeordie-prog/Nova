"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for the external agent HTTP API (auth, turn store, routes, web outbound)."""


from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dermascan.shared import agent_api_auth
from dermascan.shared.agent_turn_store import (
    load_ingress_meta,
    load_turn_reply,
    save_ingress_meta,
    save_turn_reply,
)


def _request_with_auth(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def test_agent_api_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUTIS_AGENT_API_KEY", raising=False)
    assert agent_api_auth.agent_api_key_configured() is False
    monkeypatch.setenv("QUTIS_AGENT_API_KEY", "secret")
    assert agent_api_auth.agent_api_key_configured() is True


def test_verify_bearer_skips_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUTIS_AGENT_API_KEY", raising=False)
    agent_api_auth.verify_agent_api_bearer(_request_with_auth("anything"))


def test_verify_bearer_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUTIS_AGENT_API_KEY", "expected")
    with pytest.raises(HTTPException) as exc:
        agent_api_auth.verify_agent_api_bearer(_request_with_auth("wrong"))
    assert exc.value.status_code == 401


def test_turn_store_round_trip(fake_redis: Any) -> None:
    save_ingress_meta(
        fake_redis,
        request_id="req-1",
        client_id="openclaw:session",
        case_id="case-1",
    )
    meta = load_ingress_meta(fake_redis, "req-1")
    assert meta is not None
    assert meta["client_id"] == "openclaw:session"

    save_turn_reply(
        fake_redis,
        request_id="req-1",
        client_id="openclaw:session",
        case_id="case-1",
        text="Hello",
        kind="job_reply",
    )
    reply = load_turn_reply(fake_redis, "req-1")
    assert reply is not None
    assert reply["text"] == "Hello"


def _wire_agent_api_http(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    from dermascan.backend.api import agent_routes, endpoints

    monkeypatch.setenv("REDIS_URL", "redis://fake/0")
    monkeypatch.setattr(agent_routes, "_redis", fake_redis)
    monkeypatch.setattr(endpoints, "_redis", fake_redis)
    monkeypatch.setattr(endpoints, "_enforce_ingress_capacity", AsyncMock(return_value=None))


def test_post_agent_turn_enqueues(
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from dermascan.backend.api import agent_routes
    from dermascan.backend.app.main import app

    _wire_agent_api_http(monkeypatch, fake_redis)

    client = TestClient(app)
    resp = client.post(
        "/v1/agents/turns",
        json={"client_id": "openclaw:1", "text": "Hi"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert load_ingress_meta(fake_redis, body["request_id"]) is not None
    entries = fake_redis.xrange(agent_routes._JOB_STREAM, count=10)
    assert any(fields.get("request_id") == body["request_id"] for _, fields in entries)


def test_get_agent_turn_done_from_cache(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from dermascan.backend.app.main import app

    _wire_agent_api_http(monkeypatch, fake_redis)
    save_ingress_meta(fake_redis, request_id="rid-done", client_id="c1", case_id="case-1")
    save_turn_reply(
        fake_redis,
        request_id="rid-done",
        client_id="c1",
        case_id="case-1",
        text="Done text",
    )

    client = TestClient(app)
    resp = client.get("/v1/agents/turns/rid-done")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["text"] == "Done text"


def test_get_agent_turn_404_unknown(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from dermascan.backend.app.main import app

    _wire_agent_api_http(monkeypatch, fake_redis)
    client = TestClient(app)
    resp = client.get("/v1/agents/turns/missing")
    assert resp.status_code == 404


def test_outbound_web_persists_turn_reply(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from dermascan.backend.services.agents import outbound_sender_worker

    monkeypatch.setattr(outbound_sender_worker, "_redis", fake_redis)
    outbound_sender_worker._deliver(
        {
            "channel": "web",
            "client_id": "openclaw:1",
            "case_id": "case-99",
            "request_id": "req-web",
            "text": "Assistant reply",
            "kind": "job_reply",
        }
    )
    cached = load_turn_reply(fake_redis, "req-web")
    assert cached is not None
    assert cached["text"] == "Assistant reply"


def test_post_agent_turn_requires_content(
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from dermascan.backend.app.main import app

    _wire_agent_api_http(monkeypatch, fake_redis)

    client = TestClient(app)
    resp = client.post("/v1/agents/turns", json={"client_id": "openclaw:1"})
    assert resp.status_code == 400
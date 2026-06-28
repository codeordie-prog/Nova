"""Unit tests for shared HMAC auth."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException

from dermascan.shared import hmac_auth


@pytest.fixture
def hmac_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "test-qutis-secret-for-hmac-roundtrip"
    monkeypatch.setenv("QUTIS_INTERNAL_SECRET", secret)
    return secret


class TestHmacAuth:
    def test_sign_verify_round_trip(self, hmac_secret: str) -> None:
        body = json.dumps({"case_id": "case-1"}).encode("utf-8")
        ts = str(int(time.time()))
        sig, _ = hmac_auth.signature_header(body, ts)
        hmac_auth.verify(body, sig, ts)

    def test_tampered_body_rejected(self, hmac_secret: str) -> None:
        body = b'{"case_id":"case-1"}'
        ts = str(int(time.time()))
        sig, _ = hmac_auth.signature_header(body, ts)
        with pytest.raises(HTTPException) as exc:
            hmac_auth.verify(b'{"case_id":"case-2"}', sig, ts)
        assert exc.value.status_code == 401

    def test_stale_timestamp_rejected(self, hmac_secret: str) -> None:
        body = b"{}"
        stale_ts = str(int(time.time()) - 600)
        sig, _ = hmac_auth.signature_header(body, stale_ts)
        with pytest.raises(HTTPException) as exc:
            hmac_auth.verify(body, sig, stale_ts)
        assert exc.value.status_code == 401

    def test_missing_secret_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QUTIS_INTERNAL_SECRET", raising=False)
        ts = str(int(time.time()))
        # Inbound verify() must fail closed with 401 when the secret is unset.
        with pytest.raises(HTTPException) as exc:
            hmac_auth.verify(b"{}", "sha256=deadbeef", ts)
        assert exc.value.status_code == 401
        # Outbound sign() raises a plain RuntimeError (no HTTP semantics in worker context).
        with pytest.raises(RuntimeError):
            hmac_auth.sign(b"{}", ts)

    def test_get_path_signature_round_trip(self, hmac_secret: str) -> None:
        # Bodyless GET convention: sign the request path, not an empty body.
        path = b"/v1/escalations/case-1/image"
        ts = str(int(time.time()))
        sig, _ = hmac_auth.signature_header(path, ts)
        hmac_auth.verify(path, sig, ts)
        # A signature for one path must not verify another.
        with pytest.raises(HTTPException):
            hmac_auth.verify(b"/v1/escalations/case-2/image", sig, ts)

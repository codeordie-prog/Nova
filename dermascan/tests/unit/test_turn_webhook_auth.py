"""Tests for Turn webhook ingress auth helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException

from dermascan.shared.turn_webhook_auth import (
    turn_webhook_signature_valid,
    verify_whatsapp_ingress_auth,
)


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_turn_signature_valid() -> None:
    body = b'{"messages":[]}'
    secret = "turn-secret"
    assert turn_webhook_signature_valid(
        raw_body=body,
        signature=_sign(body, secret),
        webhook_secret=secret,
    )


def test_internal_secret_auth() -> None:
    verify_whatsapp_ingress_auth(
        internal_secret_header="abc",
        expected_internal_secret="abc",
        raw_body=b"{}",
        turn_signature_header=None,
        turn_webhook_secret="turn-secret",
    )


def test_turn_signature_auth_without_internal_header() -> None:
    body = json.dumps({"messages": []}).encode()
    secret = "turn-secret"
    verify_whatsapp_ingress_auth(
        internal_secret_header=None,
        expected_internal_secret="internal-only",
        raw_body=body,
        turn_signature_header=_sign(body, secret),
        turn_webhook_secret=secret,
    )


def test_rejects_missing_auth_when_secrets_configured() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_whatsapp_ingress_auth(
            internal_secret_header="wrong",
            expected_internal_secret="expected",
            raw_body=b"{}",
            turn_signature_header=None,
            turn_webhook_secret="turn-secret",
        )
    assert exc.value.status_code == 401

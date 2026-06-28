"""HMAC service auth for Nova ↔ escalation dashboard machine-to-machine calls."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException

_REPLAY_WINDOW_SEC: int = 300


SIGNATURE_HEADER = "X-Qutis-Signature"
TIMESTAMP_HEADER = "X-Qutis-Timestamp"


def _secret_bytes() -> bytes:
    secret = os.getenv("QUTIS_INTERNAL_SECRET", "").strip()
    if not secret:
        # Raised in both signing (worker/outbound) and verifying (FastAPI) contexts.
        # `verify()` translates this to a 401 so inbound requests fail closed.
        raise RuntimeError("QUTIS_INTERNAL_SECRET is not configured")
    return secret.encode("utf-8")


def sign(body: bytes, ts: str) -> str:
    """Sign ``f"{ts}.{body}"``.

    POST callers pass the raw request body. Bodyless GET callers (e.g. the image
    proxy) MUST pass the request path bytes (``/v1/escalations/{case_id}/image``)
    so the signature is bound to the resource, not replayable across cases.
    """
    payload = f"{ts}.".encode("utf-8") + body
    return hmac.new(_secret_bytes(), payload, hashlib.sha256).hexdigest()


def signature_header(body: bytes, ts: str | None = None) -> tuple[str, str]:
    ts_value = ts if ts is not None else str(int(time.time()))
    digest = sign(body, ts_value)
    return f"sha256={digest}", ts_value


def verify(raw_body: bytes, sig_header: str | None, ts_header: str | None) -> None:
    if not sig_header or not ts_header:
        raise HTTPException(status_code=401, detail="missing HMAC headers")

    try:
        ts = int(ts_header)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc

    if abs(int(time.time()) - ts) > _REPLAY_WINDOW_SEC:
        raise HTTPException(status_code=401, detail="timestamp outside replay window")

    expected_prefix = "sha256="
    if not sig_header.startswith(expected_prefix):
        raise HTTPException(status_code=401, detail="invalid signature format")

    try:
        expected = sign(raw_body, str(ts))
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail="QUTIS_INTERNAL_SECRET is not configured") from exc
    provided = sig_header[len(expected_prefix) :]
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid signature")

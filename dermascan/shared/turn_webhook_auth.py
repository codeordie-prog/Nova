"""
Turn.io webhook HMAC verification and WhatsApp ingress auth helpers.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def turn_webhook_signature_valid(
    *,
    raw_body: bytes,
    signature: str | None,
    webhook_secret: str,
) -> bool:
    """
    Verify Turn ``X-Turn-Hook-Signature`` (HMAC-SHA256, base64 digest).

    Args:
        raw_body: Raw HTTP body bytes.
        signature: Header value from Turn.
        webhook_secret: ``TURN_WEBHOOK_SECRET`` from Turn webhook settings.

    Returns:
        True when the signature matches the body and secret.
    """
    secret = str(webhook_secret or "").strip()
    sig = str(signature or "").strip()
    if not secret or not sig:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, sig)


def verify_whatsapp_ingress_auth(
    *,
    internal_secret_header: str | None,
    expected_internal_secret: str,
    raw_body: bytes,
    turn_signature_header: str | None,
    turn_webhook_secret: str,
) -> None:
    """
    Authorize WhatsApp ingress from the internal sidecar or Turn.io directly.

    Accepts either ``X-Internal-Secret`` (relay/sidecar) or a valid Turn HMAC
    when ``TURN_WEBHOOK_SECRET`` is configured.

    Args:
        internal_secret_header: ``X-Internal-Secret`` header value.
        expected_internal_secret: Expected ``WHATSAPP_INGRESS_SECRET``.
        raw_body: Raw webhook body (required for Turn signature verification).
        turn_signature_header: ``X-Turn-Hook-Signature`` header value.
        turn_webhook_secret: Turn webhook signing secret.

    Raises:
        HTTPException: When auth fails and secrets are configured.
    """
    expected = str(expected_internal_secret or "").strip()
    turn_secret = str(turn_webhook_secret or "").strip()
    internal = str(internal_secret_header or "").strip()

    if expected and internal and hmac.compare_digest(internal, expected):
        return
    if turn_webhook_signature_valid(
        raw_body=raw_body,
        signature=turn_signature_header,
        webhook_secret=turn_secret,
    ):
        return
    if not expected and not turn_secret:
        logger.warning(
            "WHATSAPP_INGRESS_SECRET and TURN_WEBHOOK_SECRET unset; "
            "allowing /v1/webhooks/whatsapp without auth"
        )
        return
    raise HTTPException(status_code=401, detail="invalid whatsapp ingress auth")

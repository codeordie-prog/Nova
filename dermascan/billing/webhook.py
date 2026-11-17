"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
Licence: PolyForm Noncommercial License 1.0.0

Paystack webhook signature verification.

Deliberately separate from :mod:`dermascan.shared.hmac_auth`, which signs
``f"{ts}.{body}"`` with SHA256 under ``X-Qutis-Signature`` for the escalation
dashboard. Paystack is a different scheme: **SHA512 over the raw request body,
no timestamp**. Reusing the wrong verifier rejects every webhook.

The signature is computed over the exact bytes Paystack sent. Parsing the body
into a dict and re-serializing it changes key order and whitespace, which
changes the digest — so callers must pass ``await request.body()``, never a
FastAPI-parsed model.
"""


from __future__ import annotations

import hashlib
import hmac
from typing import Any, Final, Optional

from dermascan.billing.flags import paystack_secret_key

SIGNATURE_HEADER: Final[str] = "x-paystack-signature"

# Paystack's published webhook source addresses.
PAYSTACK_IPS: Final[frozenset[str]] = frozenset(
    {"52.31.139.75", "52.49.173.169", "52.214.14.220"}
)


def verify_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """
    Check a Paystack webhook signature.

    Args:
        raw_body: Exact request body bytes, unparsed.
        signature: Value of the ``x-paystack-signature`` header.

    Returns:
        bool: True when the signature matches.
    """
    if not signature:
        return False
    key = paystack_secret_key()
    if not key:
        return False
    expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def is_paystack_ip(client_ip: Optional[str]) -> bool:
    """
    Check a caller against Paystack's published addresses.

    Args:
        client_ip: Remote address of the request.

    Returns:
        bool: True when the address is one of Paystack's.
    """
    return bool(client_ip) and client_ip in PAYSTACK_IPS


def dedupe_key(payload: dict[str, Any]) -> str:
    """
    Build the key that makes a redelivered webhook a no-op.

    Paystack legitimately re-sends webhooks, and a non-200 reply earns 72 hours
    of retries. Keyed on event, reference and amount so a genuine second
    payment on the same reference is still distinguishable.

    Args:
        payload: Decoded webhook body.

    Returns:
        str: Stable dedupe key.
    """
    data = payload.get("data") or {}
    event = str(payload.get("event") or "unknown")
    reference = str(data.get("reference") or "")
    amount = str(data.get("amount") or "")
    return f"{event}:{reference}:{amount}"

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT

Paystack API client: hosted checkout, M-PESA STK charge, and verification.

Two things here cost real money if they are wrong:

* **Amounts are in subunits.** KES 100 is ``10000``. Passing major units
  charges a hundredth of the intended amount, and Paystack will not complain.
* **Verification is mandatory before granting credit.** The webhook says a
  payment succeeded; only ``/transaction/verify`` proves it, and Paystack's own
  documentation warns about double-fulfilment for digital value.

The transaction reference is the tracking code that ties money back to an
account. Whether the user pays by card in a browser or by STK on their phone,
the reference on the webhook resolves to exactly one account — nothing here
depends on knowing their phone number in advance.
"""


from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Final, Optional

import requests

from dermascan.backend.utils.backoff import (
    get_with_outbound_retry,
    post_with_outbound_retry,
)
from dermascan.billing.flags import paystack_secret_key
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

BASE_URL: Final[str] = "https://api.paystack.co"
REQUEST_TIMEOUT_S: Final[float] = 20.0
MAX_RETRIES: Final[int] = 2
BASE_DELAY_S: Final[float] = 0.5

# Paystack accepts alphanumerics plus -, . and = in a reference. Underscores
# are not in that set, so segments are hyphen-separated.
REFERENCE_PREFIX: Final[str] = "qutis"

CREDITS_PER_MAJOR_UNIT: Final[int] = 1  # 1 credit == KES 1.00
SUBUNITS_PER_MAJOR_UNIT: Final[int] = 100


class PaystackError(RuntimeError):
    """Raised when Paystack returns an error or an unusable response."""


@dataclass(frozen=True)
class ChargeResult:
    """
    Outcome of initiating a payment.

    Attributes:
        reference: Our tracking reference.
        status: Paystack's status string for the attempt.
        authorization_url: Hosted checkout URL, when this was a checkout.
        display_text: Any instruction Paystack wants shown to the user.
        raw: Full decoded response payload.
    """

    reference: str
    status: str
    authorization_url: Optional[str]
    display_text: Optional[str]
    raw: dict[str, Any]


def credits_to_minor(credits: int) -> int:
    """
    Convert credits to Paystack subunits.

    Args:
        credits: Credit count (1 credit = KES 1.00).

    Returns:
        int: Amount in subunits.
    """
    return credits * CREDITS_PER_MAJOR_UNIT * SUBUNITS_PER_MAJOR_UNIT


def new_reference(account_id: str) -> str:
    """
    Build a unique, account-tagged transaction reference.

    Time-ordered so references sort chronologically in the Paystack dashboard.

    Args:
        account_id: Account uuid.

    Returns:
        str: Reference of the form ``qutis-{acct8}-{token}``.
    """
    acct8 = account_id.replace("-", "")[:8]
    token = f"{int(time.time() * 1000):x}{secrets.token_hex(4)}"
    return f"{REFERENCE_PREFIX}-{acct8}-{token}"


def account_id_prefix(reference: str) -> Optional[str]:
    """
    Extract the account fragment from a reference, for log correlation.

    Authoritative resolution is always the ``payment_intent`` row; this is a
    convenience for tracing.

    Args:
        reference: Transaction reference.

    Returns:
        Optional[str]: The 8-character account fragment, or None.
    """
    parts = reference.split("-")
    return parts[1] if len(parts) >= 3 and parts[0] == REFERENCE_PREFIX else None


def billing_email(account_id: str, email: Optional[str]) -> str:
    """
    Email to present to Paystack, which requires one.

    Uses the address the user gave if there is one; otherwise a stable
    per-account placeholder, so a user is never blocked from paying just
    because we have not asked for their email yet.

    Args:
        account_id: Account uuid.
        email: Address the user supplied, if any.

    Returns:
        str: Email address for the transaction.
    """
    if email and "@" in email:
        return email
    return f"{account_id.replace('-', '')[:12]}@qutis.app"


def _headers() -> dict[str, str]:
    """
    Build authenticated request headers.

    Returns:
        dict[str, str]: Headers including the bearer token.

    Raises:
        PaystackError: When no secret key is configured.
    """
    key = paystack_secret_key()
    if not key:
        raise PaystackError("PAYSTACK_SECRET_KEY is not set")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _paystack_error_from(exc: requests.HTTPError) -> PaystackError:
    """
    Turn a raw HTTP error into one carrying Paystack's own explanation.

    The retry helper calls ``raise_for_status()`` on a non-retryable response,
    which discards the body — so a 400 arrives as "Bad Request" with no reason.
    Paystack puts the actual cause in the JSON envelope, and that is the part
    worth reading: "mobile money is not available in test mode" is a very
    different problem from a malformed request.

    Args:
        exc: The HTTP error raised by the retry helper.

    Returns:
        PaystackError: Same failure, with Paystack's message attached.
    """
    response = exc.response
    if response is None:
        return PaystackError(str(exc))

    detail = ""
    try:
        body = response.json()
        # The useful sentence lives in data.message; the top-level message is
        # often just "Charge attempted", which explains nothing.
        nested = body.get("data")
        if isinstance(nested, dict) and nested.get("message"):
            detail = str(nested["message"])
        else:
            detail = str(body.get("message") or "")
    except ValueError:
        detail = response.text[:300]

    return PaystackError(
        f"Paystack returned {response.status_code}: {detail or 'no message'}"
    )


def _decode(response: requests.Response) -> dict[str, Any]:
    """
    Decode and validate a Paystack response envelope.

    Args:
        response: HTTP response.

    Returns:
        dict[str, Any]: The ``data`` object.

    Raises:
        PaystackError: When the envelope reports failure or is malformed.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise PaystackError(f"non-JSON response from Paystack: {response.text[:200]}") from exc

    if not body.get("status"):
        raise PaystackError(f"Paystack rejected the request: {body.get('message')}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise PaystackError("Paystack response had no data object")
    return data


def initialize_transaction(
    *,
    email: str,
    amount_minor: int,
    currency: str,
    reference: str,
    callback_url: str,
    metadata: dict[str, Any],
) -> ChargeResult:
    """
    Create a hosted checkout so the user can pay by card or mobile money.

    Args:
        email: Payer email.
        amount_minor: Amount in subunits.
        currency: ISO currency code.
        reference: Our tracking reference.
        callback_url: Where Paystack returns the user afterwards.
        metadata: Extra context echoed back on the webhook.

    Returns:
        ChargeResult: Contains the ``authorization_url`` to send to the user.
    """
    payload: dict[str, Any] = {
        "email": email,
        "amount": amount_minor,
        "currency": currency,
        "reference": reference,
        "channels": ["mobile_money", "card"],
        "metadata": metadata,
    }
    if callback_url:
        payload["callback_url"] = callback_url

    try:
        response = post_with_outbound_retry(
            url=f"{BASE_URL}/transaction/initialize",
            max_retries=MAX_RETRIES,
            base_delay_s=BASE_DELAY_S,
            json=payload,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.HTTPError as exc:
        raise _paystack_error_from(exc) from exc
    data = _decode(response)
    logger.info("paystack checkout initialized: reference=%s", reference)
    return ChargeResult(
        reference=reference,
        status="initialized",
        authorization_url=data.get("authorization_url"),
        display_text=None,
        raw=data,
    )


def charge_mobile_money(
    *,
    email: str,
    amount_minor: int,
    currency: str,
    reference: str,
    msisdn: str,
    metadata: dict[str, Any],
) -> ChargeResult:
    """
    Send an M-PESA STK prompt to a number the user supplied.

    Args:
        email: Payer email.
        amount_minor: Amount in subunits.
        currency: ISO currency code.
        reference: Our tracking reference.
        msisdn: M-PESA number in E.164, as volunteered by the user.
        metadata: Extra context echoed back on the webhook.

    Returns:
        ChargeResult: ``status`` is typically ``pay_offline`` once the prompt is sent.
    """
    try:
        response = post_with_outbound_retry(
            url=f"{BASE_URL}/charge",
            max_retries=MAX_RETRIES,
            base_delay_s=BASE_DELAY_S,
            json={
                "email": email,
                "amount": amount_minor,
                "currency": currency,
                "reference": reference,
                "mobile_money": {"phone": msisdn, "provider": "mpesa"},
                "metadata": metadata,
            },
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.HTTPError as exc:
        raise _paystack_error_from(exc) from exc
    data = _decode(response)
    status = str(data.get("status") or "pending")
    logger.info("paystack stk charge sent: reference=%s status=%s", reference, status)
    return ChargeResult(
        reference=reference,
        status=status,
        authorization_url=None,
        display_text=data.get("display_text"),
        raw=data,
    )


def verify(reference: str) -> dict[str, Any]:
    """
    Re-check a transaction with Paystack. Always called before granting credit.

    Args:
        reference: Our tracking reference.

    Returns:
        dict[str, Any]: The verified transaction object.
    """
    try:
        response = get_with_outbound_retry(
            url=f"{BASE_URL}/transaction/verify/{reference}",
            max_retries=MAX_RETRIES,
            base_delay_s=BASE_DELAY_S,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.HTTPError as exc:
        raise _paystack_error_from(exc) from exc
    return _decode(response)

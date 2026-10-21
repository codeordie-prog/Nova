"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT

Payment intents: our record of a top-up attempt, keyed by tracking reference.

An intent is written *before* Paystack is called, so a payment can never
arrive for a reference we have never heard of. The intent is what resolves a
webhook back to an account — not the payer's phone number, which we may never
know.
"""


from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

STATUS_INITIALIZED = "initialized"
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"

UNRESOLVED_STATUSES = (STATUS_INITIALIZED, STATUS_PENDING)

METHOD_CHECKOUT = "checkout"
METHOD_STK = "stk"


def create(
    *,
    account_id: str,
    reference: str,
    amount_minor: int,
    currency: str,
    credits_to_grant: int,
    method: str,
    msisdn: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record a top-up attempt before calling Paystack.

    Args:
        account_id: Account uuid.
        reference: Our tracking reference.
        amount_minor: Amount in subunits.
        currency: ISO currency code.
        credits_to_grant: Credits to add once the payment verifies.
        method: ``checkout`` or ``stk``.
        msisdn: Number the user gave, for STK attempts.

    Returns:
        dict[str, Any]: The inserted intent row.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            INSERT INTO billing.payment_intent (
                account_id, reference, amount_minor, currency,
                credits_to_grant, status, method, msisdn
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                account_id,
                reference,
                amount_minor,
                currency,
                credits_to_grant,
                STATUS_INITIALIZED,
                method,
                msisdn,
            ),
        ).fetchone()
    logger.info(
        "payment intent created: reference=%s account_id=%s credits=%s method=%s",
        reference, account_id, credits_to_grant, method,
    )
    return dict(row)


def get_by_reference(reference: str) -> Optional[dict[str, Any]]:
    """
    Look up an intent by tracking reference.

    Args:
        reference: Our tracking reference.

    Returns:
        Optional[dict]: The intent row, or None when unknown.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM billing.payment_intent WHERE reference = %s", (reference,)
        ).fetchone()
    return None if row is None else dict(row)


def update_status(
    reference: str,
    status: str,
    *,
    channel_used: Optional[str] = None,
    authorization_url: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
) -> None:
    """
    Move an intent to a new status.

    Args:
        reference: Our tracking reference.
        status: New status value.
        channel_used: How the user actually paid, once known.
        authorization_url: Hosted checkout URL, for checkout intents.
        raw: Latest payload from Paystack, stored for support and audit.
    """
    with transaction() as conn:
        conn.execute(
            """
            UPDATE billing.payment_intent
            SET status = %s,
                channel_used = COALESCE(%s, channel_used),
                authorization_url = COALESCE(%s, authorization_url),
                raw = COALESCE(%s, raw),
                updated_at = now()
            WHERE reference = %s
            """,
            (
                status,
                channel_used,
                authorization_url,
                Jsonb(raw) if raw is not None else None,
                reference,
            ),
        )
    logger.info("payment intent status: reference=%s status=%s", reference, status)


def unresolved(*, min_age_s: int, max_age_s: int, limit: int = 50) -> list[dict[str, Any]]:
    """
    List intents the verify poller should re-check.

    Webhooks fire only on success, so a pending M-PESA charge the user never
    completed would otherwise sit unresolved forever.

    Args:
        min_age_s: Ignore intents younger than this; the webhook may still land.
        max_age_s: Intents older than this are abandoned instead.
        limit: Page size.

    Returns:
        list[dict]: Intent rows, oldest first.
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT * FROM billing.payment_intent
            WHERE status = ANY(%s)
              AND created_at <= now() - make_interval(secs => %s)
              AND created_at >  now() - make_interval(secs => %s)
            ORDER BY created_at
            LIMIT %s
            """,
            (list(UNRESOLVED_STATUSES), min_age_s, max_age_s, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def expire_stale(*, max_age_s: int) -> int:
    """
    Mark long-unresolved intents abandoned.

    Args:
        max_age_s: Age past which an intent is given up on.

    Returns:
        int: Number of intents abandoned.
    """
    with transaction() as conn:
        result = conn.execute(
            """
            UPDATE billing.payment_intent
            SET status = %s, updated_at = now()
            WHERE status = ANY(%s)
              AND created_at <= now() - make_interval(secs => %s)
            """,
            (STATUS_ABANDONED, list(UNRESOLVED_STATUSES), max_age_s),
        )
        return result.rowcount

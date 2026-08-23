"""
Credit accounts keyed by ``(channel, client_id)``.

One wallet per messaging identity. Auto-created on first billable ingress.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from dermascan.billing.config import (
    BILLING_FREE_GRANT_CREDITS,
    BILLING_RATE_CARD_VERSION,
    BillingRefType,
    LedgerKind,
)
from dermascan.billing.db import transaction
from dermascan.billing.ledger import append


@dataclass(frozen=True)
class Account:
    """Credit wallet for one messaging identity."""

    id: str
    channel: str
    client_id: str
    balance_credits: int
    currency: str
    free_credits_granted: bool


def _row_to_account(row: dict[str, Any]) -> Account:
    """Map a ``billing.account`` row to :class:`Account`."""
    return Account(
        id=str(row["id"]),
        channel=str(row["channel"]),
        client_id=str(row["client_id"]),
        balance_credits=int(row["balance_credits"]),
        currency=str(row["currency"]),
        free_credits_granted=bool(row["free_credits_granted"]),
    )


def get_or_create_account(channel: str, client_id: str) -> Account:
    """
    Fetch or create the wallet for a messaging identity.

    Args:
        channel: ``whatsapp`` or ``telegram``.
        client_id: Channel-native user identifier.

    Returns:
        Account: Existing or newly created account (with optional free grant).
    """
    with transaction() as conn:
        row = conn.execute(
            """
            INSERT INTO billing.account (id, channel, client_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (channel, client_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            (str(uuid.uuid4()), channel, client_id),
        ).fetchone()
        account = _row_to_account(row)

        if not account.free_credits_granted and BILLING_FREE_GRANT_CREDITS > 0:
            grant_result = append(
                conn,
                account_id=account.id,
                delta_credits=BILLING_FREE_GRANT_CREDITS,
                kind=LedgerKind.GRANT.value,
                ref_type=BillingRefType.MANUAL.value,
                ref_id=account.id,
                idempotency_key=f"grant:free:{account.id}",
                metadata={
                    "reason": "free_trial",
                    "rate_card": BILLING_RATE_CARD_VERSION,
                },
            )
            if grant_result.applied:
                conn.execute(
                    """
                    UPDATE billing.account
                    SET free_credits_granted = true, updated_at = now()
                    WHERE id = %s
                    """,
                    (account.id,),
                )
                account = Account(
                    id=account.id,
                    channel=account.channel,
                    client_id=account.client_id,
                    balance_credits=grant_result.balance_after,
                    currency=account.currency,
                    free_credits_granted=True,
                )
    return account


def get_balance(account_id: str) -> int:
    """
    Read current spendable balance for an account.

    Args:
        account_id: Account uuid.

    Returns:
        int: ``balance_credits``, or 0 when the account is unknown.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT balance_credits FROM billing.account WHERE id = %s",
            (account_id,),
        ).fetchone()
    return 0 if row is None else int(row["balance_credits"])

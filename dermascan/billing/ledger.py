"""
Append-only credit ledger for the meter/hold lane.

Every balance mutation is a ledger row under ``SELECT … FOR UPDATE`` on the
account. Paystack top-ups use the same table with ``kind=topup``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from psycopg import Connection

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class LedgerAppendResult:
    """Outcome of an append-only ledger write."""

    applied: bool
    balance_after: int
    ledger_id: int


def _json(payload: dict[str, Any]) -> str:
    """Serialize metadata for psycopg jsonb cast."""
    return json.dumps(payload)


def append(
    conn: Connection,
    *,
    account_id: str,
    delta_credits: int,
    kind: str,
    ref_type: str,
    ref_id: str,
    idempotency_key: str,
    job_type: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> LedgerAppendResult:
    """
    Append one ledger entry and move ``balance_credits`` by ``delta_credits``.

    Args:
        conn: Open connection already inside a transaction.
        account_id: Account uuid.
        delta_credits: Positive to credit, negative to debit.
        kind: Ledger kind (see :class:`LedgerKind`).
        ref_type: Reference type (see :class:`BillingRefType`).
        ref_id: Identifier within ``ref_type``.
        idempotency_key: Unique dedupe key; replays are no-ops.
        job_type: Optional deliverable type.
        metadata: Optional json metadata.

    Returns:
        LedgerAppendResult: Whether the entry was new and the resulting balance.

    Raises:
        ValueError: When the account does not exist.
    """
    locked = conn.execute(
        "SELECT balance_credits FROM billing.account WHERE id = %s FOR UPDATE",
        (account_id,),
    ).fetchone()
    if locked is None:
        raise ValueError(f"unknown billing account: {account_id}")

    existing = conn.execute(
        "SELECT id, balance_after FROM billing.ledger WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        logger.info(
            "ledger replay ignored: key=%s account_id=%s",
            idempotency_key,
            account_id,
        )
        return LedgerAppendResult(
            applied=False,
            balance_after=int(existing["balance_after"]),
            ledger_id=int(existing["id"]),
        )

    balance_after = int(locked["balance_credits"]) + delta_credits
    inserted = conn.execute(
        """
        INSERT INTO billing.ledger (
            account_id, delta_credits, balance_after, kind, job_type,
            ref_type, ref_id, idempotency_key, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (
            account_id,
            delta_credits,
            balance_after,
            kind,
            job_type,
            ref_type,
            ref_id,
            idempotency_key,
            _json(metadata or {}),
        ),
    ).fetchone()

    conn.execute(
        """
        UPDATE billing.account
        SET balance_credits = %s, updated_at = now()
        WHERE id = %s
        """,
        (balance_after, account_id),
    )
    return LedgerAppendResult(
        applied=True,
        balance_after=balance_after,
        ledger_id=int(inserted["id"]),
    )

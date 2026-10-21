"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT

The append-only credit ledger. The only writer of ``account.balance_credits``.

Two rules hold this together:

* **Every balance change is a ledger row first.** ``balance_credits`` is a
  cached sum, and the nightly reconciliation asserts it equals
  ``SUM(ledger.delta_credits)``. Any drift is a P1.
* **Balance updates are relative, never absolute.** ``balance = balance + delta``
  under a row lock. An absolute write computed from an earlier read would
  clobber a top-up that committed in between — the exact bug that loses a
  paying customer's money.

Replays are expected, not exceptional: Paystack re-sends webhooks and Redis
streams redeliver. ``idempotency_key`` makes a repeat a no-op that returns the
original outcome.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

KIND_TOPUP: Final[str] = "topup"
KIND_DEBIT: Final[str] = "debit"
KIND_GRANT: Final[str] = "grant"
KIND_REFUND: Final[str] = "refund"
KIND_ADJUSTMENT: Final[str] = "adjustment"
KIND_HOLD: Final[str] = "hold"
KIND_RELEASE: Final[str] = "release"


@dataclass(frozen=True)
class LedgerResult:
    """
    Outcome of a ledger append.

    Attributes:
        applied: False when the idempotency key had already been recorded.
        balance_after: Account balance after the entry (or after the original
            entry, on a replay).
        ledger_id: Row id of the entry that owns this outcome.
    """

    applied: bool
    balance_after: int
    ledger_id: int


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
) -> LedgerResult:
    """
    Append one ledger entry and move the balance by ``delta_credits``.

    Must be called inside a transaction. Takes a row lock on the account so
    concurrent entries for the same account serialize rather than interleave.

    Args:
        conn: Open connection already inside a transaction.
        account_id: Account uuid.
        delta_credits: Positive to credit, negative to debit.
        kind: One of the ``KIND_*`` constants.
        ref_type: What this entry refers to (``payment``, ``trip``, ``manual``, ``grant``).
        ref_id: Identifier within that ref_type.
        idempotency_key: Unique key; a repeat is a no-op.
        job_type: Deliverable this entry paid for, when applicable.
        metadata: Extra context stored as jsonb.

    Returns:
        LedgerResult: Whether the entry was new, and the resulting balance.

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
            "ledger replay ignored: key=%s account_id=%s", idempotency_key, account_id
        )
        return LedgerResult(
            applied=False,
            balance_after=int(existing["balance_after"]),
            ledger_id=int(existing["id"]),
        )

    balance_after = int(locked["balance_credits"]) + delta_credits

    inserted = conn.execute(
        """
        INSERT INTO billing.ledger (
            account_id, delta_credits, balance_after, kind,
            job_type, ref_type, ref_id, idempotency_key, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            Jsonb(metadata or {}),
        ),
    ).fetchone()

    conn.execute(
        """
        UPDATE billing.account
        SET balance_credits = balance_credits + %s, updated_at = now()
        WHERE id = %s
        """,
        (delta_credits, account_id),
    )

    logger.info(
        "ledger entry: account_id=%s delta=%s kind=%s balance_after=%s",
        account_id, delta_credits, kind, balance_after,
    )
    return LedgerResult(applied=True, balance_after=balance_after, ledger_id=int(inserted["id"]))


def append_standalone(**kwargs: Any) -> LedgerResult:
    """
    Append a ledger entry in its own transaction.

    Args:
        **kwargs: As :func:`append`, without ``conn``.

    Returns:
        LedgerResult: Outcome of the append.
    """
    with transaction() as conn:
        return append(conn, **kwargs)


def history(
    account_id: str, *, limit: int = 50, before_id: Optional[int] = None
) -> list[dict[str, Any]]:
    """
    Read an account's ledger, newest first.

    Args:
        account_id: Account uuid.
        limit: Page size.
        before_id: Keyset cursor; return entries older than this id.

    Returns:
        list[dict]: Ledger rows.
    """
    sql = "SELECT * FROM billing.ledger WHERE account_id = %s"
    params: list[Any] = [account_id]
    if before_id is not None:
        sql += " AND id < %s"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    with transaction() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def verify_balance(account_id: str) -> tuple[int, int]:
    """
    Compare the cached balance against the ledger sum.

    Args:
        account_id: Account uuid.

    Returns:
        tuple[int, int]: (cached balance, ledger sum). Equal means healthy.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT a.balance_credits AS cached,
                   COALESCE(SUM(l.delta_credits), 0) AS summed
            FROM billing.account a
            LEFT JOIN billing.ledger l ON l.account_id = a.id
            WHERE a.id = %s
            GROUP BY a.balance_credits
            """,
            (account_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown billing account: {account_id}")
    return int(row["cached"]), int(row["summed"])


# The meter lane names this type LedgerAppendResult; same shape, same writer.
LedgerAppendResult = LedgerResult

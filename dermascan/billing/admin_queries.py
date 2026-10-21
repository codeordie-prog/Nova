"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT

Read-only aggregates for the admin billing portal.

The dashboard has its own Postgres and never touches this one; it consumes
these through the HMAC-signed ``/v1/admin/billing/*`` endpoints. Nova stays the
single owner of billing data, which keeps a billing migration from becoming a
dashboard migration.

Nothing here returns conversation content, images, or diagnoses. Clinical
material belongs to the escalation dashboard under its own consent model, and
an admin looking at revenue has no business reading it.
"""


from __future__ import annotations

from typing import Any, Optional

from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _page_size(limit: Optional[int]) -> int:
    """
    Clamp a caller-supplied page size.

    Args:
        limit: Requested size.

    Returns:
        int: Size within bounds.
    """
    if not limit or limit < 1:
        return DEFAULT_PAGE_SIZE
    return min(limit, MAX_PAGE_SIZE)


def mask_client_id(client_id: str, channel: str) -> str:
    """
    Mask a client identifier for display.

    On WhatsApp the client_id is a phone number, which is PII in a
    health-adjacent product. Masked by default; revealing is a deliberate
    action the portal audits.

    Args:
        client_id: Raw identifier.
        channel: Channel the identifier belongs to.

    Returns:
        str: Masked identifier.
    """
    value = str(client_id or "")
    if channel != "whatsapp" or len(value) < 7:
        return value
    return f"{value[:5]}{'•' * (len(value) - 8)}{value[-3:]}"


def overview(*, days: int = 30) -> dict[str, Any]:
    """
    Headline numbers for the portal landing page.

    Args:
        days: Window for the trend figures.

    Returns:
        dict[str, Any]: Totals, revenue, and payment health.
    """
    with transaction() as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*)                                        AS accounts,
                COALESCE(SUM(balance_credits), 0)               AS outstanding_credits,
                COUNT(*) FILTER (WHERE free_credits_granted)    AS trials_granted
            FROM billing.account
            """
        ).fetchone()

        flows = conn.execute(
            """
            SELECT
                COALESCE(SUM(delta_credits) FILTER (WHERE kind = 'topup'), 0)      AS credits_sold,
                COALESCE(-SUM(delta_credits) FILTER (WHERE kind = 'debit'), 0)     AS credits_consumed,
                COALESCE(SUM(delta_credits) FILTER (WHERE kind = 'grant'), 0)      AS credits_granted,
                COALESCE(SUM(delta_credits) FILTER (
                    WHERE kind = 'topup' AND created_at >= now() - make_interval(days => %s)
                ), 0)                                                              AS credits_sold_window
            FROM billing.ledger
            """,
            (days,),
        ).fetchone()

        payments = conn.execute(
            """
            SELECT
                status,
                COUNT(*)                     AS count,
                COALESCE(SUM(amount_minor), 0) AS amount_minor
            FROM billing.payment_intent
            GROUP BY status
            """
        ).fetchall()

        by_job = conn.execute(
            """
            SELECT job_type, COUNT(*) AS jobs, COALESCE(-SUM(delta_credits), 0) AS credits
            FROM billing.ledger
            WHERE kind = 'debit' AND job_type IS NOT NULL
            GROUP BY job_type
            ORDER BY credits DESC
            """
        ).fetchall()

    payment_status = {row["status"]: dict(row) for row in payments}
    revenue_kes = int(flows["credits_sold"])

    return {
        "window_days": days,
        "accounts": int(totals["accounts"]),
        "trials_granted": int(totals["trials_granted"]),
        "outstanding_credits": int(totals["outstanding_credits"]),
        "credits_sold": revenue_kes,
        "credits_sold_window": int(flows["credits_sold_window"]),
        "credits_consumed": int(flows["credits_consumed"]),
        "credits_granted": int(flows["credits_granted"]),
        # 1 credit = KES 1.00, so credits sold is revenue in KES.
        "revenue_kes": revenue_kes,
        "payments": payment_status,
        "failed_payments": int(payment_status.get("failed", {}).get("count", 0)),
        "pending_payments": int(payment_status.get("pending", {}).get("count", 0)),
        "by_job_type": [dict(row) for row in by_job],
    }


def list_accounts(
    *, limit: Optional[int] = None, cursor: Optional[str] = None, sort: str = "recent"
) -> dict[str, Any]:
    """
    Page through accounts for the users table.

    Args:
        limit: Page size.
        cursor: Opaque keyset cursor from a previous page.
        sort: ``recent``, ``balance``, or ``spend``.

    Returns:
        dict[str, Any]: Rows plus the next cursor.
    """
    size = _page_size(limit)
    order = {
        "balance": "a.balance_credits DESC, a.id",
        "spend": "lifetime_spend DESC, a.id",
        "recent": "a.updated_at DESC, a.id",
    }.get(sort, "a.updated_at DESC, a.id")

    with transaction() as conn:
        rows = conn.execute(
            f"""
            SELECT
                a.id, a.channel, a.client_id, a.balance_credits, a.currency,
                a.free_credits_granted, a.created_at, a.updated_at,
                COALESCE(-SUM(l.delta_credits) FILTER (WHERE l.kind = 'debit'), 0) AS lifetime_spend,
                COALESCE(SUM(l.delta_credits) FILTER (WHERE l.kind = 'topup'), 0)  AS lifetime_topup,
                MAX(l.created_at)                                                  AS last_activity
            FROM billing.account a
            LEFT JOIN billing.ledger l ON l.account_id = a.id
            WHERE (%s::text IS NULL OR a.id::text > %s)
            GROUP BY a.id
            ORDER BY {order}
            LIMIT %s
            """,
            (cursor, cursor, size + 1),
        ).fetchall()

    items = [dict(row) for row in rows[:size]]
    for item in items:
        item["client_id_masked"] = mask_client_id(item["client_id"], item["channel"])
        item.pop("client_id", None)

    return {
        "items": items,
        "next_cursor": str(rows[size]["id"]) if len(rows) > size else None,
    }


def account_detail(account_id: str) -> Optional[dict[str, Any]]:
    """
    One account, with lifetime figures.

    Args:
        account_id: Account uuid.

    Returns:
        Optional[dict]: Account summary, or None when unknown.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT
                a.*,
                COALESCE(-SUM(l.delta_credits) FILTER (WHERE l.kind = 'debit'), 0) AS lifetime_spend,
                COALESCE(SUM(l.delta_credits) FILTER (WHERE l.kind = 'topup'), 0)  AS lifetime_topup,
                COUNT(l.id) FILTER (WHERE l.kind = 'debit')                        AS billed_jobs
            FROM billing.account a
            LEFT JOIN billing.ledger l ON l.account_id = a.id
            WHERE a.id = %s
            GROUP BY a.id
            """,
            (account_id,),
        ).fetchone()

        if row is None:
            return None

        by_job = conn.execute(
            """
            SELECT job_type, COUNT(*) AS jobs, COALESCE(-SUM(delta_credits), 0) AS credits
            FROM billing.ledger
            WHERE account_id = %s AND kind = 'debit' AND job_type IS NOT NULL
            GROUP BY job_type
            ORDER BY credits DESC
            """,
            (account_id,),
        ).fetchall()

    detail = dict(row)
    detail["client_id_masked"] = mask_client_id(detail["client_id"], detail["channel"])
    detail["by_job_type"] = [dict(r) for r in by_job]
    return detail


def account_ledger(
    account_id: str, *, limit: Optional[int] = None, before_id: Optional[int] = None
) -> dict[str, Any]:
    """
    Page an account's ledger, newest first.

    Args:
        account_id: Account uuid.
        limit: Page size.
        before_id: Keyset cursor.

    Returns:
        dict[str, Any]: Entries plus the next cursor.
    """
    size = _page_size(limit)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, delta_credits, balance_after, kind, job_type,
                   ref_type, ref_id, metadata, created_at
            FROM billing.ledger
            WHERE account_id = %s AND (%s::bigint IS NULL OR id < %s)
            ORDER BY id DESC
            LIMIT %s
            """,
            (account_id, before_id, before_id, size + 1),
        ).fetchall()

    items = [dict(row) for row in rows[:size]]
    return {
        "items": items,
        "next_cursor": int(rows[size]["id"]) if len(rows) > size else None,
    }


def list_payments(
    *, status: Optional[str] = None, limit: Optional[int] = None
) -> dict[str, Any]:
    """
    Payment intents for the payments screen.

    Args:
        status: Restrict to one status.
        limit: Page size.

    Returns:
        dict[str, Any]: Payment rows.
    """
    size = _page_size(limit)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.reference, p.amount_minor, p.currency, p.credits_to_grant,
                   p.status, p.method, p.channel_used, p.created_at, p.updated_at,
                   a.channel, a.client_id
            FROM billing.payment_intent p
            JOIN billing.account a ON a.id = p.account_id
            WHERE (%s::text IS NULL OR p.status = %s)
            ORDER BY p.created_at DESC
            LIMIT %s
            """,
            (status, status, size),
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["client_id_masked"] = mask_client_id(item["client_id"], item["channel"])
        item.pop("client_id", None)
        items.append(item)
    return {"items": items}


def reconciliation_drift(*, limit: int = 50) -> list[dict[str, Any]]:
    """
    Accounts whose cached balance disagrees with their ledger.

    Any row here is a P1: the balance is a cached sum and must equal
    ``SUM(ledger.delta_credits)``.

    Args:
        limit: Maximum rows to return.

    Returns:
        list[dict]: Drifting accounts, worst first.
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.channel, a.balance_credits AS cached,
                   COALESCE(SUM(l.delta_credits), 0) AS summed
            FROM billing.account a
            LEFT JOIN billing.ledger l ON l.account_id = a.id
            GROUP BY a.id
            HAVING a.balance_credits <> COALESCE(SUM(l.delta_credits), 0)
            ORDER BY ABS(a.balance_credits - COALESCE(SUM(l.delta_credits), 0)) DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]

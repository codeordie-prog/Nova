"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
Licence: PolyForm Noncommercial License 1.0.0

Credit accounts, keyed by (channel, client_id).

One account per messaging identity. The same human on WhatsApp and Telegram
gets two accounts today; ``owner_id`` is the seam for merging them later
without a schema change.
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from psycopg import Connection

from dermascan.billing import ledger, pricing
from dermascan.billing.db import run_transaction, transaction
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

logger = get_logger(__name__)

_CHANNEL_MEMO_TTL_S: int = 30 * 24 * 3600


@dataclass(frozen=True)
class Account:
    """
    A credit account.

    Attributes:
        id: Account uuid.
        channel: ``whatsapp``, ``telegram``, ``web``, or ``internal``.
        client_id: Channel-native identifier (phone or Telegram user id).
        balance_credits: Spendable credits; may be negative within the overdraft floor.
        currency: Settlement currency.
        email: Collected at first top-up, required by Paystack.
        msisdn: M-PESA number, only when the user volunteered one.
        free_credits_granted: Whether the one-time trial grant has been made.
    """

    id: str
    channel: str
    client_id: str
    balance_credits: int
    currency: str
    email: Optional[str]
    msisdn: Optional[str]
    free_credits_granted: bool


def _row_to_account(row: dict[str, Any]) -> Account:
    """
    Map a database row to an :class:`Account`.

    Args:
        row: Row from ``billing.account``.

    Returns:
        Account: Mapped account.
    """
    return Account(
        id=str(row["id"]),
        channel=row["channel"],
        client_id=row["client_id"],
        balance_credits=int(row["balance_credits"]),
        currency=row["currency"],
        email=row.get("email"),
        msisdn=row.get("msisdn"),
        free_credits_granted=bool(row["free_credits_granted"]),
    )


def get_or_create(channel: str, client_id: str) -> Account:
    """
    Fetch the account for a messaging identity, creating it on first contact.

    Args:
        channel: ``whatsapp``, ``telegram``, ``web``, or ``internal``.
        client_id: Channel-native identifier.

    Returns:
        Account: Existing or newly created account.
    """
    def _upsert(conn: Connection) -> Account:
        row = conn.execute(
            """
            INSERT INTO billing.account (channel, client_id)
            VALUES (%s, %s)
            ON CONFLICT (channel, client_id) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            (channel, client_id),
        ).fetchone()
        return _row_to_account(row)

    return run_transaction(_upsert)


def get_by_id(account_id: str) -> Optional[Account]:
    """
    Fetch an account by uuid.

    Args:
        account_id: Account uuid.

    Returns:
        Optional[Account]: The account, or None when unknown.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM billing.account WHERE id = %s", (account_id,)
        ).fetchone()
    return None if row is None else _row_to_account(row)


def grant_free_trial(account_id: str) -> ledger.LedgerResult:
    """
    Make the one-time free-credit grant.

    Idempotent twice over: the ``free_credits_granted`` flag short-circuits it,
    and the ledger idempotency key would reject a second grant anyway. New
    users must see the product work before they meet a paywall.

    Args:
        account_id: Account uuid.

    Returns:
        LedgerResult: The grant, or a no-op result when already granted.
    """
    credits = pricing.free_trial_credits()
    with transaction() as conn:
        row = conn.execute(
            "SELECT free_credits_granted, balance_credits FROM billing.account WHERE id = %s FOR UPDATE",
            (account_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown billing account: {account_id}")
        if row["free_credits_granted"] or credits <= 0:
            return ledger.LedgerResult(
                applied=False, balance_after=int(row["balance_credits"]), ledger_id=0
            )

        result = ledger.append(
            conn,
            account_id=account_id,
            delta_credits=credits,
            kind=ledger.KIND_GRANT,
            ref_type="grant",
            ref_id="free_trial",
            idempotency_key=f"free_trial:{account_id}",
            metadata={"reason": "first_contact"},
        )
        conn.execute(
            "UPDATE billing.account SET free_credits_granted = true, updated_at = now() WHERE id = %s",
            (account_id,),
        )

    logger.info("free trial granted: account_id=%s credits=%s", account_id, credits)
    return result


def set_contact(
    account_id: str, *, email: Optional[str] = None, msisdn: Optional[str] = None
) -> None:
    """
    Record contact details the user supplied.

    Both values come from what the user typed or entered at checkout. Nothing
    here reads a phone number out of a channel payload.

    Args:
        account_id: Account uuid.
        email: Email address, when known.
        msisdn: M-PESA number in E.164, when the user volunteered one.
    """
    if email is None and msisdn is None:
        return
    with transaction() as conn:
        conn.execute(
            """
            UPDATE billing.account
            SET email = COALESCE(%s, email),
                msisdn = COALESCE(%s, msisdn),
                updated_at = now()
            WHERE id = %s
            """,
            (email, msisdn, account_id),
        )


def resolve_for_client(channel: str, client_id: str) -> Account:
    """
    Get the account for a client, granting the free trial on first contact.

    This is the single entry point the chat surface uses.

    Args:
        channel: ``whatsapp``, ``telegram``, ``web``, or ``internal``.
        client_id: Channel-native identifier.

    Returns:
        Account: Account with any first-contact grant already applied.
    """
    account = get_or_create(channel, client_id)
    if not account.free_credits_granted:
        grant_free_trial(account.id)
        refreshed = get_by_id(account.id)
        if refreshed is not None:
            return refreshed
    return account


def remember_channel(client_id: str, channel: str) -> None:
    """
    Note which channel a client arrived on.

    Accounts are keyed on (channel, client_id), but tools running inside the
    orchestrator only receive ``client_id``. The worker knows the channel from
    the job envelope, so it records it here for the tools to read back.

    Args:
        client_id: Channel-native identifier.
        channel: ``whatsapp``, ``telegram``, ``web``, or ``internal``.
    """
    if not client_id or channel not in {"whatsapp", "telegram", "web", "internal"}:
        return
    try:
        get_redis_client(decode_responses=True).set(
            f"billing:channel:{client_id}", channel, ex=_CHANNEL_MEMO_TTL_S
        )
    except Exception:
        logger.exception("channel memo write failed: client_id=%s", client_id)


def resolve_channel(client_id: str) -> Optional[str]:
    """
    Recall the channel a client arrived on.

    Falls back to the account table, so a returning user whose memo has
    expired still resolves.

    Args:
        client_id: Channel-native identifier.

    Returns:
        Optional[str]: The channel, or None when it cannot be determined.
    """
    try:
        cached = get_redis_client(decode_responses=True).get(f"billing:channel:{client_id}")
        if cached:
            return str(cached)
    except Exception:
        logger.exception("channel memo read failed: client_id=%s", client_id)

    with transaction() as conn:
        rows = conn.execute(
            "SELECT channel FROM billing.account WHERE client_id = %s", (client_id,)
        ).fetchall()
    return rows[0]["channel"] if len(rows) == 1 else None


def get_balance(account_id: str) -> int:
    """
    Read the current spendable balance.

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


# The meter lane calls this name. It must stay pointed at resolve_for_client so
# there is exactly one path that makes the free-trial grant, sized from
# billing.price_list rather than from the environment.
get_or_create_account = resolve_for_client

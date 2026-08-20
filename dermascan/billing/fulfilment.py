"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Turning a verified Paystack payment into credits.

The order here is not negotiable:

1. **Re-verify with Paystack.** A webhook is a notification, not proof.
   Paystack's own documentation warns about double-fulfilment for digital
   value, and a forged or replayed webhook must not mint credits.
2. **Check the amount and currency against our intent.** A webhook that says
   a different amount than the intent we created is not a payment we asked for.
3. **Write Postgres first, then Redis.** Postgres is authoritative for money.
   If the process dies between the two, the balance is durably correct and the
   next cache hydration picks it up; the reverse ordering would credit a cache
   that could be flushed.

Every step is idempotent, because all three of Paystack retries, stream
redelivery, and the verify poller can drive this for the same reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from dermascan.billing import accounts, intents, ledger, paystack_client, redis_wallet
from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

OUTCOME_GRANTED = "granted"
OUTCOME_ALREADY = "already_fulfilled"
OUTCOME_UNKNOWN_REFERENCE = "unknown_reference"
OUTCOME_NOT_SUCCESSFUL = "not_successful"
OUTCOME_MISMATCH = "amount_mismatch"


@dataclass(frozen=True)
class Fulfilment:
    """
    Result of attempting to fulfil a payment.

    Attributes:
        outcome: One of the ``OUTCOME_*`` constants.
        reference: Tracking reference.
        account_id: Account credited, when known.
        credits_granted: Credits added by this call (0 on a replay).
        balance_after: Postgres balance after fulfilment.
        pending_task: What the user was blocked on, if anything.
    """

    outcome: str
    reference: str
    account_id: Optional[str] = None
    credits_granted: int = 0
    balance_after: int = 0
    pending_task: Optional[str] = None

    @property
    def granted(self) -> bool:
        """Whether this call actually added credits."""
        return self.outcome == OUTCOME_GRANTED


def fulfil(reference: str) -> Fulfilment:
    """
    Verify a payment and credit the account.

    Safe to call repeatedly for the same reference; only the first call that
    sees a successful verification adds credits.

    Args:
        reference: Our tracking reference.

    Returns:
        Fulfilment: What happened, and what to tell the user.
    """
    intent = intents.get_by_reference(reference)
    if intent is None:
        logger.warning("payment for unknown reference: %s", reference)
        return Fulfilment(outcome=OUTCOME_UNKNOWN_REFERENCE, reference=reference)

    account_id = str(intent["account_id"])
    if intent["status"] == intents.STATUS_SUCCESS:
        return Fulfilment(
            outcome=OUTCOME_ALREADY,
            reference=reference,
            account_id=account_id,
            balance_after=_current_balance(account_id),
        )

    data = paystack_client.verify(reference)
    status = str(data.get("status") or "")
    if status != "success":
        new_status = (
            intents.STATUS_FAILED if status in {"failed", "abandoned"} else intents.STATUS_PENDING
        )
        intents.update_status(reference, new_status, raw=data)
        return Fulfilment(
            outcome=OUTCOME_NOT_SUCCESSFUL, reference=reference, account_id=account_id
        )

    if not _amount_matches(intent, data):
        logger.error(
            "payment amount mismatch: reference=%s expected=%s/%s got=%s/%s",
            reference, intent["amount_minor"], intent["currency"],
            data.get("amount"), data.get("currency"),
        )
        intents.update_status(reference, intents.STATUS_FAILED, raw=data)
        return Fulfilment(
            outcome=OUTCOME_MISMATCH, reference=reference, account_id=account_id
        )

    credits = int(intent["credits_to_grant"])

    # Postgres and the intent move together, so a crash cannot leave a paid
    # intent uncredited or a credited account with a pending intent.
    with transaction() as conn:
        result = ledger.append(
            conn,
            account_id=account_id,
            delta_credits=credits,
            kind=ledger.KIND_TOPUP,
            ref_type="payment",
            ref_id=reference,
            idempotency_key=f"payment:{reference}",
            metadata={
                "channel_used": data.get("channel"),
                "paid_at": str(data.get("paid_at") or ""),
                "amount_minor": intent["amount_minor"],
            },
        )
        conn.execute(
            """
            UPDATE billing.payment_intent
            SET status = %s, channel_used = %s, updated_at = now()
            WHERE reference = %s
            """,
            (intents.STATUS_SUCCESS, data.get("channel"), reference),
        )

    if not result.applied:
        return Fulfilment(
            outcome=OUTCOME_ALREADY,
            reference=reference,
            account_id=account_id,
            balance_after=result.balance_after,
        )

    # Cache last: a lost Redis write is repaired by the next hydration.
    redis_wallet.apply_topup(account_id, credits=credits, reference=reference)

    email = data.get("customer", {}).get("email") if isinstance(data.get("customer"), dict) else None
    if email and not str(email).endswith("@qutis.app"):
        accounts.set_contact(account_id, email=str(email))

    logger.info(
        "payment fulfilled: reference=%s account_id=%s credits=%s balance=%s",
        reference, account_id, credits, result.balance_after,
    )
    return Fulfilment(
        outcome=OUTCOME_GRANTED,
        reference=reference,
        account_id=account_id,
        credits_granted=credits,
        balance_after=result.balance_after,
        pending_task=redis_wallet.take_pending_task(account_id),
    )


def _amount_matches(intent: dict[str, Any], data: dict[str, Any]) -> bool:
    """
    Check a verified transaction against the intent we created.

    Args:
        intent: Stored payment intent row.
        data: Verified transaction object from Paystack.

    Returns:
        bool: True when amount and currency both match.
    """
    return (
        int(data.get("amount") or 0) == int(intent["amount_minor"])
        and str(data.get("currency") or "").upper() == str(intent["currency"]).upper()
    )


def _current_balance(account_id: str) -> int:
    """
    Read the authoritative balance from Postgres.

    Args:
        account_id: Account uuid.

    Returns:
        int: Balance in credits, 0 when the account is unknown.
    """
    account = accounts.get_by_id(account_id)
    return account.balance_credits if account is not None else 0

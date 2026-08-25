"""
Hold → settle reservations for the meter/hold lane.

Orchestrator v1 keys on ``request_id``. MedGemma uses ``medgemma_job_id`` (Phase 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Optional

from dermascan.billing.config import (
    BILLING_RATE_CARD_VERSION,
    BillingJobType,
    BillingRefType,
    LedgerKind,
    ReservationState,
    micro_to_credits,
    min_balance_credits,
    orchestrator_hold_credits,
    reservation_ttl_sec,
)
from dermascan.billing.db import transaction
from dermascan.billing.ledger import append
from dermascan.billing.usage import sum_usage_micro_for_scope
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

INSUFFICIENT_CREDITS_MESSAGE: Final[str] = (
    "You have run out of credits. Reply *top up* when you are ready to add more "
    "and continue your session."
)


class InsufficientCreditsError(Exception):
    """Raised when an account cannot satisfy a hold or minimum balance check."""

    def __init__(self, *, balance: int, required: int) -> None:
        self.balance = balance
        self.required = required
        super().__init__(f"insufficient credits: balance={balance} required={required}")


@dataclass(frozen=True)
class Reservation:
    """Hold record for one billable scope."""

    id: str
    account_id: str
    reservation_key: str
    job_type: str
    hold_credits: int
    state: str

# Place a hold/reserve credits on the account for the given reservation key
def place_hold(
    *,
    account_id: str,
    reservation_key: str,
    job_type: str = BillingJobType.ORCHESTRATOR_TURN.value,
    hold_credits: Optional[int] = None,
    case_id: Optional[str] = None,
    client_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> Reservation:
    """
    Reserve a credit ceiling for one billable scope.

    Args:
        account_id: Account uuid.
        reservation_key: Idempotency scope (e.g. ``request_id``).
        job_type: Billable job type label.
        hold_credits: Maximum credits to hold. Defaults to the
            ``orchestrator_hold_credits`` setting.
        case_id: Optional clinical case id.
        client_id: Optional client id for support queries.
        channel: Optional channel for support queries.

    Returns:
        Reservation: New or existing reservation for ``reservation_key``.

    Raises:
        InsufficientCreditsError: When balance is below hold + minimum floor.
        ValueError: When hold_credits is non-positive or account is unknown.
    """
    if hold_credits is None:
        hold_credits = orchestrator_hold_credits()
    if hold_credits <= 0:
        raise ValueError("hold_credits must be positive")

    required = hold_credits + min_balance_credits()

    with transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM billing.reservation WHERE reservation_key = %s",
            (reservation_key,),
        ).fetchone()
        if existing is not None:
            return Reservation(
                id=str(existing["id"]),
                account_id=str(existing["account_id"]),
                reservation_key=str(existing["reservation_key"]),
                job_type=str(existing["job_type"]),
                hold_credits=int(existing["hold_credits"]),
                state=str(existing["state"]),
            )

        locked = conn.execute(
            "SELECT balance_credits FROM billing.account WHERE id = %s FOR UPDATE",
            (account_id,),
        ).fetchone()
        if locked is None:
            raise ValueError(f"unknown billing account: {account_id}")

        balance = int(locked["balance_credits"])
        if balance < required:
            raise InsufficientCreditsError(balance=balance, required=required)

        reservation_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=reservation_ttl_sec()
        )

        conn.execute(
            """
            INSERT INTO billing.reservation (
                id, account_id, reservation_key, job_type, hold_credits,
                state, case_id, client_id, channel, expires_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                reservation_id,
                account_id,
                reservation_key,
                job_type,
                hold_credits,
                ReservationState.HELD.value,
                case_id,
                client_id,
                channel,
                expires_at,
            ),
        )

        append(
            conn,
            account_id=account_id,
            delta_credits=-hold_credits,
            kind=LedgerKind.HOLD.value,
            ref_type=BillingRefType.RESERVATION.value,
            ref_id=reservation_id,
            idempotency_key=f"hold:{reservation_id}",
            job_type=job_type,
            metadata={
                "reservation_key": reservation_key,
                "hold_credits": hold_credits,
                "rate_card": BILLING_RATE_CARD_VERSION,
            },
        )

    logger.info(
        "billing hold placed: reservation_key=%s hold=%s account_id=%s",
        reservation_key,
        hold_credits,
        account_id,
    )
    return Reservation(
        id=reservation_id,
        account_id=account_id,
        reservation_key=reservation_key,
        job_type=job_type,
        hold_credits=hold_credits,
        state=ReservationState.HELD.value,
    )


def commit_reservation(reservation_id: str) -> int:
    """
    Settle a held reservation from accumulated usage lines.

    Args:
        reservation_id: Reservation uuid.

    Returns:
        int: Actual credits charged.

    Raises:
        ValueError: When the reservation does not exist.
    """
    with transaction() as conn:
        reservation = conn.execute(
            "SELECT * FROM billing.reservation WHERE id = %s FOR UPDATE",
            (reservation_id,),
        ).fetchone()
        if reservation is None:
            raise ValueError(f"unknown reservation: {reservation_id}")

        state = str(reservation["state"])
        if state == ReservationState.COMMITTED.value:
            return int(reservation["actual_credits"] or 0)
        if state == ReservationState.RELEASED.value:
            return 0

        account_id = str(reservation["account_id"])
        scope_key = str(reservation["reservation_key"])
        hold_credits = int(reservation["hold_credits"])
        job_type = str(reservation["job_type"])

        # Round the scope total once. Rounding each usage line would charge a
        # whole shilling per LLM call and multiply the error by the number of
        # calls in the turn.
        actual = micro_to_credits(sum_usage_micro_for_scope(conn, scope_key))
        refund = max(0, hold_credits - actual)
        overrun = max(0, actual - hold_credits)

        if refund > 0:
            append(
                conn,
                account_id=account_id,
                delta_credits=refund,
                kind=LedgerKind.RELEASE.value,
                ref_type=BillingRefType.RESERVATION.value,
                ref_id=reservation_id,
                idempotency_key=f"release:{reservation_id}",
                job_type=job_type,
                metadata={
                    "scope_key": scope_key,
                    "hold_credits": hold_credits,
                    "actual_credits": actual,
                    "refund_credits": refund,
                },
            )

        if overrun > 0:
            append(
                conn,
                account_id=account_id,
                delta_credits=-overrun,
                kind=LedgerKind.DEBIT.value,
                ref_type=BillingRefType.USAGE_SETTLEMENT.value,
                ref_id=reservation_id,
                idempotency_key=f"debit:overrun:{reservation_id}",
                job_type=job_type,
                metadata={"scope_key": scope_key, "overrun_credits": overrun},
            )

        conn.execute(
            """
            UPDATE billing.reservation
            SET state = %s, actual_credits = %s, settled_at = now()
            WHERE id = %s
            """,
            (ReservationState.COMMITTED.value, actual, reservation_id),
        )

    logger.info(
        "billing reservation committed: id=%s actual=%s hold=%s",
        reservation_id,
        actual,
        hold_credits,
    )
    return actual


def get_reservation_id_by_key(reservation_key: str) -> Optional[str]:
    """
    Look up a reservation uuid by its scope key.

    Args:
        reservation_key: MedGemma ``job_id`` or orchestrator ``request_id``.

    Returns:
        Optional[str]: Reservation uuid when present.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM billing.reservation WHERE reservation_key = %s",
            (reservation_key,),
        ).fetchone()
    return None if row is None else str(row["id"])


def release_reservation(
    reservation_id: str,
    *,
    reason: str = "turn_failed_or_cancelled",
) -> int:
    """
    Cancel a held reservation and refund the full hold.

    Args:
        reservation_id: Reservation uuid.
        reason: Audit label stored on the release ledger row.

    Returns:
        int: Credits refunded.

    Raises:
        ValueError: When the reservation does not exist.
    """
    with transaction() as conn:
        reservation = conn.execute(
            "SELECT * FROM billing.reservation WHERE id = %s FOR UPDATE",
            (reservation_id,),
        ).fetchone()
        if reservation is None:
            raise ValueError(f"unknown reservation: {reservation_id}")

        state = str(reservation["state"])
        if state != ReservationState.HELD.value:
            return 0

        account_id = str(reservation["account_id"])
        hold_credits = int(reservation["hold_credits"])
        job_type = str(reservation["job_type"])

        append(
            conn,
            account_id=account_id,
            delta_credits=hold_credits,
            kind=LedgerKind.RELEASE.value,
            ref_type=BillingRefType.RESERVATION.value,
            ref_id=reservation_id,
            idempotency_key=f"release:cancel:{reservation_id}",
            job_type=job_type,
            metadata={"reason": reason},
        )

        conn.execute(
            """
            UPDATE billing.reservation
            SET state = %s, actual_credits = 0, settled_at = now()
            WHERE id = %s
            """,
            (ReservationState.RELEASED.value, reservation_id),
        )

    logger.info(
        "billing reservation released: id=%s refund=%s",
        reservation_id,
        hold_credits,
    )
    return hold_credits


def expire_stale_holds(*, limit: int = 100) -> int:
    """
    Release held reservations past ``expires_at``.

    Args:
        limit: Maximum reservations to process per sweep.

    Returns:
        int: Number of reservations released.
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id FROM billing.reservation
            WHERE state = %s AND expires_at < now()
            ORDER BY expires_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (ReservationState.HELD.value, limit),
        ).fetchall()

    released = 0
    for row in rows:
        release_reservation(str(row["id"]), reason="expired_ttl")
        released += 1
        logger.info(
            "billing stale hold expired: reservation_id=%s",
            str(row["id"]),
        )
    return released

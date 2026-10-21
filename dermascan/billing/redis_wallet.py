"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Qutis Team
Date: 2026-08-20

The Redis credit wallet: the hot path for spending, and the contract the
orchestrator's deduction checkpoints call into.

**Postgres owns money; Redis owns the live spendable balance.** A trip deducts
several times (orchestrator LLM call, each tool call, MedGemma inference) and
settles to Postgres once, so checkpoint count stops driving write volume.

Three keys per account, hash-tagged so Redis Cluster keeps them on one slot and
a single Lua script can touch all three:

===========================  ==========================================
``bal:{acct}``               live spendable balance, already decremented
``unsettled:{acct}``         deducted but not yet written to Postgres
``trip:{acct}:{request_id}`` per-checkpoint breakdown for one trip
===========================  ==========================================

Truth is ``postgres.balance - unsettled``. Losing Redis forgives in-flight
spend rather than inventing money, which is the correct direction to fail.

Two invariants that are easy to get wrong:

* **The gate reads ``bal`` alone.** ``unsettled`` is a reconciliation counter,
  not a second pool; subtracting it again would charge the user twice.
* **A cold key is not a zero balance.** :data:`COLD` tells the caller to
  hydrate from Postgres. Treating a cache miss as "no credits" locks out
  paying users.
"""


from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final, Optional

import redis

from dermascan.billing import ledger
from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

logger = get_logger(__name__)

# deduct() outcome codes.
COLD: Final[int] = -1
INSUFFICIENT: Final[int] = 0
DEDUCTED: Final[int] = 1

TRIP_TTL_S: Final[int] = 3600
APPLIED_TTL_S: Final[int] = 7 * 24 * 3600
OPEN_TRIPS_KEY: Final[str] = "billing:trips:open"
PENDING_TASK_TTL_S: Final[int] = 24 * 3600

# Deduct atomically: check, decrement, and record the checkpoint in one step.
# Split across commands this would race two concurrent trips into overspending.
_DEDUCT_LUA: Final[str] = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {-1, 0} end
local bal = tonumber(raw)
local cost = tonumber(ARGV[1])
if bal - cost < tonumber(ARGV[3]) then return {0, bal} end
bal = redis.call('DECRBY', KEYS[1], cost)
redis.call('INCRBY', KEYS[2], cost)
redis.call('HINCRBY', KEYS[3], ARGV[2], cost)
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[4]))
return {1, bal}
"""

# Apply a verified top-up. INCRBY (relative) rather than SET (absolute) so it
# commutes with any deduction landing at the same moment. A cold balance is
# left alone: hydration will read the already-updated Postgres value.
_APPLY_TOPUP_LUA: Final[str] = """
if redis.call('SADD', KEYS[2], ARGV[2]) == 0 then
  return {0, tonumber(redis.call('GET', KEYS[1]) or '-1')}
end
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[3]))
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {1, redis.call('INCRBY', KEYS[1], ARGV[1])}
end
return {1, -1}
"""


@dataclass(frozen=True)
class DeductResult:
    """
    Outcome of one checkpoint deduction.

    Attributes:
        code: :data:`DEDUCTED`, :data:`INSUFFICIENT`, or :data:`COLD`.
        balance: Balance after the deduction; meaningless when cold.
    """

    code: int
    balance: int

    @property
    def ok(self) -> bool:
        """Whether credits were taken."""
        return self.code == DEDUCTED

    @property
    def is_cold(self) -> bool:
        """Whether the caller must hydrate and retry."""
        return self.code == COLD


def balance_key(account_id: str) -> str:
    """Redis key for an account's live balance."""
    return f"bal:{{{account_id}}}"


def unsettled_key(account_id: str) -> str:
    """Redis key for an account's unsettled total."""
    return f"unsettled:{{{account_id}}}"


def trip_key(account_id: str, request_id: str) -> str:
    """Redis key for one trip's checkpoint breakdown."""
    return f"trip:{{{account_id}}}:{request_id}"


def applied_key(account_id: str) -> str:
    """Redis key for the set of top-up references already applied."""
    return f"applied:{{{account_id}}}"


def pending_task_key(account_id: str) -> str:
    """Redis key for the task a user was turned away from."""
    return f"pending_task:{{{account_id}}}"


def _client() -> redis.Redis:
    """
    Return the shared Redis client.

    Returns:
        redis.Redis: Decoded-response client.
    """
    return get_redis_client(decode_responses=True)


def hydrate(account_id: str) -> int:
    """
    Load an account's spendable balance into Redis from Postgres.

    Called on a cold miss. ``unsettled`` is subtracted because those credits
    are already spent but not yet written to Postgres.

    Args:
        account_id: Account uuid.

    Returns:
        int: The balance now in Redis.

    Raises:
        ValueError: When the account does not exist.
    """
    client = _client()
    with transaction() as conn:
        row = conn.execute(
            "SELECT balance_credits FROM billing.account WHERE id = %s", (account_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown billing account: {account_id}")

    unsettled = int(client.get(unsettled_key(account_id)) or 0)
    balance = int(row["balance_credits"]) - unsettled
    client.set(balance_key(account_id), balance)
    logger.info(
        "wallet hydrated: account_id=%s balance=%s unsettled=%s",
        account_id, balance, unsettled,
    )
    return balance


def get_balance(account_id: str, *, hydrate_if_cold: bool = True) -> int:
    """
    Read the live spendable balance.

    Args:
        account_id: Account uuid.
        hydrate_if_cold: Load from Postgres when the key is missing.

    Returns:
        int: Spendable credits.
    """
    raw = _client().get(balance_key(account_id))
    if raw is None:
        return hydrate(account_id) if hydrate_if_cold else 0
    return int(raw)


def deduct(
    account_id: str,
    *,
    credits: int,
    checkpoint: str,
    request_id: str,
    overdraft_floor: int = 0,
) -> DeductResult:
    """
    Take credits for one checkpoint. The orchestrator's entry point.

    Deduction is destructive: once taken, the credits are gone from ``bal`` and
    no concurrent trip can spend them. There is no separate held pool.

    Args:
        account_id: Account uuid.
        credits: Credits to take.
        checkpoint: Which meter fired, e.g. ``llm_call``, ``tool_call``, ``medgemma``.
        request_id: Trip identifier; groups checkpoints for settlement.
        overdraft_floor: Lowest balance the deduction may leave. Negative lets
            an in-flight trip finish rather than abandoning work already paid for.

    Returns:
        DeductResult: Outcome and resulting balance.
    """
    client = _client()
    outcome = client.eval(
        _DEDUCT_LUA,
        3,
        balance_key(account_id),
        unsettled_key(account_id),
        trip_key(account_id, request_id),
        credits,
        checkpoint,
        overdraft_floor,
        TRIP_TTL_S,
    )
    code, balance = int(outcome[0]), int(outcome[1])
    if code == DEDUCTED:
        client.zadd(
            OPEN_TRIPS_KEY, {f"{account_id}:{request_id}": _now_ms()}, nx=True
        )
    elif code == INSUFFICIENT:
        logger.info(
            "deduction refused: account_id=%s credits=%s balance=%s",
            account_id, credits, balance,
        )
    return DeductResult(code=code, balance=balance)


def _now_ms() -> int:
    """Current epoch milliseconds."""
    return int(time.time() * 1000)


def settle(account_id: str, request_id: str, *, job_type: Optional[str] = None) -> int:
    """
    Write one trip's spend to Postgres and clear its Redis state.

    One database write per trip regardless of how many checkpoints fired. Safe
    to call twice: the ledger idempotency key makes the second a no-op.

    Args:
        account_id: Account uuid.
        request_id: Trip identifier.
        job_type: Deliverable this trip produced, for margin reporting.

    Returns:
        int: Credits settled.
    """
    client = _client()
    key = trip_key(account_id, request_id)
    breakdown = client.hgetall(key) or {}
    total = sum(int(v) for v in breakdown.values())

    if total > 0:
        with transaction() as conn:
            ledger.append(
                conn,
                account_id=account_id,
                delta_credits=-total,
                kind=ledger.KIND_DEBIT,
                ref_type="trip",
                ref_id=request_id,
                idempotency_key=f"trip:{request_id}",
                job_type=job_type,
                metadata={"checkpoints": {k: int(v) for k, v in breakdown.items()}},
            )
        client.decrby(unsettled_key(account_id), total)

    client.delete(key)
    client.zrem(OPEN_TRIPS_KEY, f"{account_id}:{request_id}")
    logger.info("trip settled: account_id=%s request_id=%s credits=%s", account_id, request_id, total)
    return total


def reap_stale_trips(*, max_age_s: int = 1800, limit: int = 100) -> int:
    """
    Settle trips whose worker died before doing it.

    Without this, ``unsettled`` stays inflated forever and reconciliation shows
    permanent drift. Deleting the trip key on a TTL instead would destroy the
    evidence rather than bank the debt.

    Args:
        max_age_s: Age past which an open trip is settled forcibly.
        limit: Maximum trips to settle in one sweep.

    Returns:
        int: Number of trips settled.
    """
    client = _client()
    cutoff = _now_ms() - max_age_s * 1000
    stale = client.zrangebyscore(OPEN_TRIPS_KEY, 0, cutoff, start=0, num=limit)
    settled = 0
    for entry in stale:
        account_id, _, request_id = str(entry).partition(":")
        if not request_id:
            client.zrem(OPEN_TRIPS_KEY, entry)
            continue
        try:
            settle(account_id, request_id)
            settled += 1
        except Exception:
            logger.exception("stale trip settle failed: %s", entry)
    if settled:
        logger.warning("reaped %s stale trip(s)", settled)
    return settled


def apply_topup(account_id: str, *, credits: int, reference: str) -> int:
    """
    Add purchased credits to the live balance.

    Relative, so it commutes with any deduction happening at the same instant —
    a top-up landing mid-trip needs no coordination with that trip.

    Args:
        account_id: Account uuid.
        credits: Credits purchased.
        reference: Payment reference, used to make retries idempotent.

    Returns:
        int: Balance after applying, or -1 when the balance was cold.
    """
    outcome = _client().eval(
        _APPLY_TOPUP_LUA,
        2,
        balance_key(account_id),
        applied_key(account_id),
        credits,
        reference,
        APPLIED_TTL_S,
    )
    applied, balance = int(outcome[0]), int(outcome[1])
    if not applied:
        logger.info("topup already applied to wallet: reference=%s", reference)
    return balance


def set_pending_task(account_id: str, description: str) -> None:
    """
    Remember what a user was turned away from, so it can be offered back.

    Args:
        account_id: Account uuid.
        description: Short human-readable description of the blocked task.
    """
    _client().set(pending_task_key(account_id), description, ex=PENDING_TASK_TTL_S)


def take_pending_task(account_id: str) -> Optional[str]:
    """
    Read and clear the pending task, if any.

    Args:
        account_id: Account uuid.

    Returns:
        Optional[str]: The description, or None when nothing was pending.
    """
    client = _client()
    key = pending_task_key(account_id)
    value = client.get(key)
    if value is not None:
        client.delete(key)
    return value


def wallet_state(account_id: str) -> dict[str, Any]:
    """
    Snapshot of an account's Redis wallet, for the admin portal and debugging.

    Args:
        account_id: Account uuid.

    Returns:
        dict[str, Any]: Balance, unsettled total, and open trip count.
    """
    client = _client()
    raw_balance = client.get(balance_key(account_id))
    return {
        "balance": None if raw_balance is None else int(raw_balance),
        "cold": raw_balance is None,
        "unsettled": int(client.get(unsettled_key(account_id)) or 0),
        "open_trips": len(
            [
                entry
                for entry in client.zrange(OPEN_TRIPS_KEY, 0, -1)
                if str(entry).startswith(f"{account_id}:")
            ]
        ),
    }

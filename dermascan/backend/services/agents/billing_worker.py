"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT
Description:
Billing worker: consume billing:jobs, fulfil verified payments, and run the
periodic sweeps that stop money going quietly missing.

Three loops, each closing a different failure mode:

* **Stream consumer** — a webhook arrived; verify it and credit the account.
* **Verify poller** — Paystack only fires webhooks on success, so an STK charge
  the user never completed would sit ``pending`` forever. This re-checks
  unresolved intents and eventually abandons them.
* **Trip reaper** — a worker that dies mid-trip leaves credits deducted in Redis
  but never written to Postgres. This settles the debt rather than letting
  ``unsettled`` drift upward forever.
"""


from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Final

from dermascan.backend.services.agents import reclaim_logic
from dermascan.billing import accounts, fulfilment, intents, notify, redis_wallet
from dermascan.billing.flags import billing_enabled
from dermascan.shared.load_env import ensure_env_loaded
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import (
    check_redis_connectivity,
    get_redis_client,
    run_with_redis_retries,
)

ensure_env_loaded()

logger = get_logger("agents")

BILLING_STREAM: Final[str] = os.getenv("BILLING_JOB_STREAM", "billing:jobs")
BILLING_DLQ_STREAM: Final[str] = os.getenv("BILLING_DLQ_STREAM", "billing:dlq")
BILLING_CONSUMER_GROUP: Final[str] = os.getenv("BILLING_CONSUMER_GROUP", "billing-workers")
BILLING_CONSUMER_NAME: Final[str] = (
    f"{os.getenv('BILLING_CONSUMER_NAME', 'billing-worker')}"
    f"-{socket.gethostname()}-{os.getpid()}"
)

STREAM_BATCH_SIZE: Final[int] = int(os.getenv("WORKERS_STREAM_BATCH_SIZE", "10"))
STREAM_BLOCK_MS: Final[int] = int(os.getenv("WORKERS_STREAM_BLOCK_MS", "5000"))
WORKERS_STREAM_MAXLEN: Final[int] = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

# Give the webhook a head start before polling, so the common path is not
# duplicated by the fallback.
VERIFY_POLL_INTERVAL_S: Final[int] = int(os.getenv("BILLING_VERIFY_POLL_INTERVAL_S", "120"))
VERIFY_MIN_AGE_S: Final[int] = int(os.getenv("BILLING_VERIFY_MIN_AGE_S", "180"))
VERIFY_MAX_AGE_S: Final[int] = int(os.getenv("BILLING_VERIFY_MAX_AGE_S", "1800"))

TRIP_REAP_INTERVAL_S: Final[int] = int(os.getenv("BILLING_TRIP_REAP_INTERVAL_S", "300"))
TRIP_MAX_AGE_S: Final[int] = int(os.getenv("BILLING_TRIP_MAX_AGE_S", "1800"))

_redis = get_redis_client(decode_responses=True)
reclaim_logic.set_redis_client(_redis)


def _ensure_consumer_group() -> None:
    """Create the consumer group, tolerating an existing one."""
    try:
        _redis.xgroup_create(BILLING_STREAM, BILLING_CONSUMER_GROUP, id="0", mkstream=True)
        logger.info(
            "billing consumer group created: stream=%s group=%s",
            BILLING_STREAM, BILLING_CONSUMER_GROUP,
        )
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


def _notify_outcome(result: fulfilment.Fulfilment) -> None:
    """
    Tell the user what happened to their payment.

    Args:
        result: Outcome from :func:`dermascan.billing.fulfilment.fulfil`.
    """
    if result.account_id is None:
        return
    account = accounts.get_by_id(result.account_id)
    if account is None:
        return

    if result.granted:
        text = notify.payment_confirmed_message(
            credits=result.credits_granted,
            balance=result.balance_after,
            pending_task=result.pending_task,
        )
    elif result.outcome in {fulfilment.OUTCOME_MISMATCH, fulfilment.OUTCOME_NOT_SUCCESSFUL}:
        text = notify.payment_failed_message()
    else:
        return

    notify.enqueue(
        client_id=account.client_id,
        channel=account.channel,
        text=text,
        request_id=result.reference,
        dedupe_discriminator=result.outcome,
    )


def _process_message(msg_id: str, fields: dict[str, Any]) -> None:
    """
    Fulfil the payment a webhook told us about.

    Args:
        msg_id: Stream message id.
        fields: Stream fields, carrying the transaction reference.
    """
    reference = str(fields.get("reference") or "").strip()
    try:
        if not reference:
            logger.warning("billing job without reference: msg_id=%s", msg_id)
            return

        result = fulfilment.fulfil(reference)
        _mark_event(fields.get("dedupe_key"), result.outcome)
        _notify_outcome(result)
    except Exception:
        logger.exception("billing job failed: reference=%s", reference)
        _dlq(msg_id, fields)
    finally:
        _redis.xack(BILLING_STREAM, BILLING_CONSUMER_GROUP, msg_id)
        _redis.xdel(BILLING_STREAM, msg_id)


def _mark_event(dedupe_key: Any, outcome: str) -> None:
    """
    Record how a stored webhook resolved.

    Args:
        dedupe_key: Key of the ``billing.paystack_event`` row.
        outcome: Fulfilment outcome string.
    """
    key = str(dedupe_key or "").strip()
    if not key:
        return
    status = "processed" if outcome in {
        fulfilment.OUTCOME_GRANTED, fulfilment.OUTCOME_ALREADY
    } else "ignored"
    try:
        from dermascan.billing.db import transaction

        with transaction() as conn:
            conn.execute(
                """
                UPDATE billing.paystack_event
                SET status = %s, processed_at = now()
                WHERE dedupe_key = %s
                """,
                (status, key),
            )
    except Exception:
        logger.exception("paystack event status update failed: dedupe=%s", key)


def _dlq(msg_id: str, fields: dict[str, Any]) -> None:
    """
    Park an unprocessable message for inspection.

    Args:
        msg_id: Original stream message id.
        fields: Original stream fields.
    """
    try:
        _redis.xadd(
            BILLING_DLQ_STREAM,
            {**fields, "failed_msg_id": msg_id},
            maxlen=WORKERS_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("billing dlq write failed: msg_id=%s", msg_id)


def _verify_poll_once() -> None:
    """Re-verify unresolved intents, then abandon the ones past saving."""
    pending = intents.unresolved(min_age_s=VERIFY_MIN_AGE_S, max_age_s=VERIFY_MAX_AGE_S)
    for intent in pending:
        reference = str(intent["reference"])
        try:
            result = fulfilment.fulfil(reference)
            if result.granted:
                logger.info("verify poller recovered a payment: reference=%s", reference)
                _notify_outcome(result)
        except Exception:
            logger.exception("verify poll failed: reference=%s", reference)

    abandoned = intents.expire_stale(max_age_s=VERIFY_MAX_AGE_S)
    if abandoned:
        logger.info("abandoned %s stale payment intent(s)", abandoned)


def _sweep_loop(
    *, name: str, interval_s: int, action, stop_event: threading.Event
) -> None:
    """
    Run a periodic sweep until asked to stop.

    Args:
        name: Sweep name, for logs.
        interval_s: Seconds between runs.
        action: Zero-argument callable to run.
        stop_event: Set to end the loop.
    """
    while not stop_event.wait(interval_s):
        try:
            action()
        except Exception:
            logger.exception("billing sweep failed: %s", name)


def _start_reclaim_thread(stop_event: threading.Event) -> threading.Thread:
    """Reclaim stale pending billing jobs (mirrors escalation/outbound workers)."""

    def _reclaim_loop() -> None:
        while not stop_event.is_set():
            try:

                def _reclaim_handler(reclaim_msg_id: str, reclaim_fields: dict[str, Any]) -> None:
                    if reclaim_logic.mark_processed(BILLING_STREAM, reclaim_msg_id):
                        try:
                            _process_message(reclaim_msg_id, reclaim_fields)
                        except Exception:
                            logger.exception(
                                "reclaim handler failed: stream=%s msg_id=%s",
                                BILLING_STREAM,
                                reclaim_msg_id,
                            )
                            _redis.delete(
                                reclaim_logic.processed_key(BILLING_STREAM, reclaim_msg_id)
                            )

                reclaimed = reclaim_logic.reclaim_stale_pending(
                    stream=BILLING_STREAM,
                    group=BILLING_CONSUMER_GROUP,
                    consumer=BILLING_CONSUMER_NAME,
                    handler=_reclaim_handler,
                    dlq_stream=BILLING_DLQ_STREAM,
                )
                if reclaimed > 0:
                    logger.info(
                        "reclaim pass completed: stream=%s reclaimed=%d",
                        BILLING_STREAM,
                        reclaimed,
                    )
            except Exception:
                logger.exception("reclaim loop error: stream=%s", BILLING_STREAM)
            stop_event.wait(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC)

    thread = threading.Thread(
        target=_reclaim_loop,
        name=f"billing-reclaim-{BILLING_STREAM}",
        daemon=True,
    )
    thread.start()
    return thread


def run_worker_forever(stop_event: threading.Event | None = None) -> None:
    """
    Consume ``billing:jobs`` and run the periodic sweeps until ``stop_event`` is set.

    Args:
        stop_event: Supervisor shutdown signal from ``do_work.py``. When omitted
            (CLI entry), a local event is used until keyboard interrupt.
    """
    stop = stop_event or threading.Event()
    if not billing_enabled():
        logger.info("billing worker not started: BILLING_ENABLED is false")
        stop.wait()
        return

    check_redis_connectivity(decode_responses=True)
    _ensure_consumer_group()
    logger.info(
        "billing worker started: stream=%s group=%s consumer=%s",
        BILLING_STREAM, BILLING_CONSUMER_GROUP, BILLING_CONSUMER_NAME,
    )

    reclaim_thread = _start_reclaim_thread(stop)
    sweeps = [
        threading.Thread(
            target=_sweep_loop,
            kwargs={
                "name": "verify_poller",
                "interval_s": VERIFY_POLL_INTERVAL_S,
                "action": _verify_poll_once,
                "stop_event": stop,
            },
            name="billing-verify-poller",
            daemon=True,
        ),
        threading.Thread(
            target=_sweep_loop,
            kwargs={
                "name": "trip_reaper",
                "interval_s": TRIP_REAP_INTERVAL_S,
                "action": lambda: redis_wallet.reap_stale_trips(max_age_s=TRIP_MAX_AGE_S),
                "stop_event": stop,
            },
            name="billing-trip-reaper",
            daemon=True,
        ),
    ]
    for thread in sweeps:
        thread.start()

    try:
        while not stop.is_set():
            try:
                resp = run_with_redis_retries(
                    lambda: _redis.xreadgroup(
                        groupname=BILLING_CONSUMER_GROUP,
                        consumername=BILLING_CONSUMER_NAME,
                        streams={BILLING_STREAM: ">"},
                        count=STREAM_BATCH_SIZE,
                        block=STREAM_BLOCK_MS,
                    ),
                    operation_name="billing_xreadgroup",
                )
            except Exception:
                logger.exception("billing xreadgroup failed")
                time.sleep(0.25)
                continue

            for _, batch in (resp or []):
                for msg_id, fields in batch:
                    _process_message(str(msg_id), fields)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        reclaim_thread.join(timeout=reclaim_logic.RECLAIM_INTERVAL_SEC + 1)
        logger.info("billing worker stopped")

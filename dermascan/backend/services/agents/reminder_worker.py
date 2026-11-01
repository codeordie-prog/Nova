"""Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Reminder worker: poll Postgres for due reminders and dispatch routine outbound or morning signals."""

from __future__ import annotations

import os
import time

from dermascan.reminders.flags import reminders_enabled
from dermascan.reminders.processor import process_due_reminder_row
from dermascan.reminders.repository import list_due_reminders
from dermascan.shared.load_env import ensure_env_loaded
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import check_redis_connectivity, get_redis_client

ensure_env_loaded()

logger = get_logger("reminder_worker")

REMINDER_TICK_INTERVAL_S: float = float(os.getenv("REMINDER_TICK_INTERVAL_S", "60"))
REMINDER_DUE_BATCH_SIZE: int = max(1, int(os.getenv("REMINDER_DUE_BATCH_SIZE", "50")))

_redis = get_redis_client(decode_responses=True)


def _tick_once() -> int:
    """
    Process one batch of due reminders.

    Returns:
        int: Number of due rows processed this tick.
    """
    due = list_due_reminders(limit=REMINDER_DUE_BATCH_SIZE)
    if not due:
        return 0
    processed = 0
    for row in due:
        try:
            outcome = process_due_reminder_row(_redis, row)
            processed += 1
            logger.info(
                "reminder tick: client_id=%s kind=%s outcome=%s",
                row.get("client_id"),
                row.get("kind"),
                outcome,
            )
        except Exception:
            logger.exception(
                "reminder tick failed: client_id=%s kind=%s id=%s",
                row.get("client_id"),
                row.get("kind"),
                row.get("id"),
            )
    return processed


def run_worker_forever() -> None:
    """
    Run the reminder scheduler loop until the process exits.

    Returns:
        None: Blocks until ``KeyboardInterrupt`` or fatal error.
    """
    if not reminders_enabled():
        logger.info("reminder worker idle: REMINDERS_ENABLED is false")
        while True:
            time.sleep(max(5.0, REMINDER_TICK_INTERVAL_S))
            if reminders_enabled():
                logger.info("reminder worker starting: REMINDERS_ENABLED became true")
                break

    check_redis_connectivity(decode_responses=True)
    logger.info(
        "reminder worker started: tick_interval_s=%s batch=%s",
        REMINDER_TICK_INTERVAL_S,
        REMINDER_DUE_BATCH_SIZE,
    )

    try:
        while True:
            if not reminders_enabled():
                time.sleep(max(5.0, REMINDER_TICK_INTERVAL_S))
                continue
            try:
                _tick_once()
            except Exception:
                logger.exception("reminder worker tick error")
            time.sleep(REMINDER_TICK_INTERVAL_S)
    except KeyboardInterrupt:
        logger.info("reminder worker stopped")

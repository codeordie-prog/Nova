"""
Background sweeper for billing reservations.

Releases held reservations past ``expires_at`` when enforcement is enabled.
"""

from __future__ import annotations

import os
import threading
from typing import Final

from dermascan.billing.flags import billing_enforce_enabled
from dermascan.billing.reservations import expire_stale_holds
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

BILLING_SWEEP_INTERVAL_SEC: Final[int] = max(
    10, int(os.getenv("BILLING_SWEEP_INTERVAL_SEC", "60"))
)
BILLING_SWEEP_BATCH_LIMIT: Final[int] = max(
    1, int(os.getenv("BILLING_SWEEP_BATCH_LIMIT", "100"))
)


def run_billing_sweeper_loop(stop_event: threading.Event) -> None:
    """
    Periodically release stale held reservations.

    Args:
        stop_event: Cooperative shutdown flag shared with worker supervisor.
    """
    logger.info(
        "billing sweeper started: interval_s=%s batch_limit=%s enforce=%s",
        BILLING_SWEEP_INTERVAL_SEC,
        BILLING_SWEEP_BATCH_LIMIT,
        billing_enforce_enabled(),
    )
    while not stop_event.is_set():
        if billing_enforce_enabled():
            try:
                released = expire_stale_holds(limit=BILLING_SWEEP_BATCH_LIMIT)
                if released > 0:
                    logger.info(
                        "billing sweeper released stale holds: count=%s",
                        released,
                    )
            except Exception:
                logger.exception("billing sweeper iteration failed")

        stop_event.wait(timeout=BILLING_SWEEP_INTERVAL_SEC)


def start_billing_sweeper_thread(
    stop_event: threading.Event,
) -> threading.Thread | None:
    """
    Start the billing sweeper when hold enforcement is enabled.

    Args:
        stop_event: Cooperative shutdown flag shared with worker supervisor.

    Returns:
        threading.Thread | None: Started daemon thread, or None when enforcement is off.
    """
    if not billing_enforce_enabled():
        logger.info("billing sweeper disabled: BILLING_ENFORCE is off")
        return None

    thread = threading.Thread(
        target=run_billing_sweeper_loop,
        args=(stop_event,),
        name="billing-sweeper",
        daemon=True,
    )
    thread.start()
    return thread

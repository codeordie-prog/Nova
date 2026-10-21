"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Run Dermascan workers in a single process (orchestrator, escalation, outbound, billing)."""


from __future__ import annotations

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

import logging
import os
import signal
import threading
import time
from functools import partial
from typing import Callable

from dermascan.backend.services.agents.billing_worker import run_worker_forever as run_billing_worker
from dermascan.backend.services.agents.escalation_worker import run_worker_forever as run_escalation_worker
from dermascan.backend.services.agents.orchestrator_worker import run_worker_forever as run_orchestrator_worker
from dermascan.backend.services.agents.outbound_sender_worker import run_worker_forever as run_outbound_sender_worker
from dermascan.billing.flags import billing_active, billing_enabled, validate_billing_flags
from dermascan.billing.sweeper import start_billing_sweeper_thread
from dermascan.shared.logger import get_logger


def _rules_index_worker_enabled() -> bool:
    return os.getenv("RULES_INDEX_WORKER_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


logger = get_logger("do_work")


def _worker_thread_target(name: str, worker_fn: Callable[[], None], stop_event: threading.Event) -> None:
    try:
        logger.info("starting worker thread: %s", name)
        worker_fn()
    except Exception:
        logger.exception("worker thread crashed: %s", name)
        stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
    )

    stop_event = threading.Event()
    if billing_active():
        validate_billing_flags()
    billing_sweeper_thread = start_billing_sweeper_thread(stop_event)

    workers: list[tuple[str, Callable[[], None]]] = [
        ("orchestrator", run_orchestrator_worker),
        ("escalation", run_escalation_worker),
        ("outbound_sender", run_outbound_sender_worker),
    ]
    if billing_enabled():
        workers.append(
            ("billing", partial(run_billing_worker, stop_event)),
        )
    if _rules_index_worker_enabled():
        from dermascan.backend.services.agents.indexing_worker import run_worker_forever as run_rules_index_worker

        workers.append(("rules_index", run_rules_index_worker))

    threads: list[threading.Thread] = []
    for name, worker_fn in workers:
        thread = threading.Thread(
            target=_worker_thread_target,
            args=(name, worker_fn, stop_event),
            name=f"worker-{name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    started = ", ".join(name for name, _ in workers)
    logger.info(
        "all worker threads started: %s (knowledge + medgemma excluded unless enabled above)",
        started,
    )

    def _handle_shutdown(_sig: int, _frame: object) -> None:
        logger.info("shutdown signal received, stopping supervisor")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        while not stop_event.is_set():
            if not all(thread.is_alive() for thread in threads):
                logger.error("one or more worker threads exited; stopping supervisor")
                stop_event.set()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received, stopping supervisor")
        stop_event.set()
    finally:
        if billing_sweeper_thread is not None:
            billing_sweeper_thread.join(timeout=2.0)
        for thread in threads:
            thread.join(timeout=2.0)
        logger.info("supervisor stopped")


if __name__ == "__main__":
    main()

"""
Reset a poisoned orchestrator conversation checkpoint.

Deletes all persisted LangGraph checkpoint state for one or more conversation
threads (keyed by ``client_id``) so the next inbound message starts from a clean
state. Use this to recover a client stuck in a recursion/tool loop.

Usage:
    poetry run python dermascan/scripts/reset_checkpoint.py <client_id> [<client_id> ...]
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from dermascan.backend.services.agents.build import open_checkpointer_url
from dermascan.backend.services.agents.memory.redis_checkpointer import delete_thread
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


def main(thread_ids: list[str]) -> int:
    """
    Delete checkpoints for the provided thread ids.

    Args:
        thread_ids: Conversation thread identifiers (client ids) to reset.

    Returns:
        int: Process exit code (0 on success, non-zero on usage/config error).
    """
    load_dotenv()

    if not thread_ids:
        logger.error("no client_id provided; usage: reset_checkpoint.py <client_id> [...]")
        return 2

    redis_url: str = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        logger.error("REDIS_URL is not configured")
        return 2

    checkpointer, checkpointer_ctx = open_checkpointer_url(redis_url)
    try:
        for thread_id in thread_ids:
            deleted: bool = delete_thread(checkpointer, thread_id)
            if deleted:
                logger.info("checkpoint reset: client_id=%s", thread_id)
            else:
                logger.warning("checkpoint reset skipped/unsupported: client_id=%s", thread_id)
    finally:
        try:
            checkpointer_ctx.__exit__(None, None, None)
        except Exception:
            logger.exception("failed to close checkpointer context")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

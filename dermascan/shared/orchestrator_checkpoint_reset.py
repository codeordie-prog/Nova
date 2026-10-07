"""
Author: Dermascan Team
Date: 2026-10-07
License: MIT

Best-effort LangGraph checkpoint reset for a conversation thread (client_id).
"""

from __future__ import annotations

import logging
import os

from dermascan.backend.services.agents.build import open_checkpointer_url
from dermascan.backend.services.agents.memory.redis_checkpointer import delete_thread
from dermascan.shared.redis_stability import get_redis_client

logger = logging.getLogger(__name__)

_DEFAULT_MARKER_PREFIX = "orchestrator:checkpoint_reset"


def reset_orchestrator_thread_checkpoint(client_id: str) -> bool:
    """
    Delete persisted orchestrator checkpoint state for a WhatsApp ``client_id`` thread.

    Also clears the checkpoint-reset restoration marker so a future Qutis session
    does not inject a stale conversation recap after MYDAWA menu exit.

    Args:
        client_id: LangGraph thread id (WhatsApp wa_id for Turn users).

    Returns:
        bool: True when delete was issued on the checkpointer, False otherwise.
    """
    thread_id = str(client_id or "").strip()
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not thread_id:
        return False
    if not redis_url:
        logger.warning("checkpoint reset skipped: REDIS_URL not configured")
        return False

    deleted = False
    try:
        checkpointer, checkpointer_ctx = open_checkpointer_url(redis_url)
        try:
            deleted = delete_thread(checkpointer, thread_id)
        finally:
            try:
                checkpointer_ctx.__exit__(None, None, None)
            except Exception:
                logger.exception("failed to close checkpointer after thread reset")

        marker_prefix = os.getenv(
            "ORCHESTRATOR_CHECKPOINT_RESET_MARKER_KEY_PREFIX",
            _DEFAULT_MARKER_PREFIX,
        ).strip()
        redis = get_redis_client(decode_responses=True)
        if redis is not None and marker_prefix:
            try:
                redis.delete(f"{marker_prefix}:{thread_id}")
            except Exception:
                logger.exception(
                    "failed to clear checkpoint reset marker: client_id=%s",
                    thread_id,
                )

        if deleted:
            logger.info("orchestrator checkpoint reset: client_id=%s", thread_id)
        else:
            logger.warning("orchestrator checkpoint reset unsupported/failed: client_id=%s", thread_id)
    except Exception:
        logger.exception("orchestrator checkpoint reset error: client_id=%s", thread_id)
        return False

    return deleted

"""
Redis checkpointer wrapper for agent state persistence.
Uses langgraph's built-in RedisSaver.

A delagated delete thread function is provided to delete the thread from the RedisSaver instance.
This is useful to reset the checkpoint of a thread in case of a poisoning/looping conversation/state.
"""

from typing import Any

from langgraph.checkpoint.redis import RedisSaver


def context_from_url(url: str) -> Any:
    """Create and return a RedisSaver context manager from a connection URL."""
    return RedisSaver.from_conn_string(url)


def setup_from_url(url: str) -> tuple[RedisSaver, Any]:
    """Open RedisSaver from URL, run setup, and return (saver, context_manager).

    Caller is responsible for eventually closing the returned context manager.
    """
    ctx = context_from_url(url)
    saver = ctx.__enter__()
    saver.setup()
    return saver, ctx


def from_url(url: str) -> RedisSaver:
    """Backward-compatible helper returning an opened/setup RedisSaver instance."""
    saver, _ = setup_from_url(url)
    return saver


def delete_thread(saver: RedisSaver, thread_id: str) -> bool:
    """
    Delete all persisted checkpoints/state for a conversation thread.

    Clears a poisoned/looping conversation so the next message for the thread
    starts from a clean state. Best-effort and safe to call repeatedly.

    Args:
        saver: An opened RedisSaver instance.
        thread_id: The conversation thread identifier (e.g. client_id).

    Returns:
        bool: True if a delete was issued, False if unsupported/failed.
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    delete_fn = getattr(saver, "delete_thread", None)
    if not callable(delete_fn):
        return False
    delete_fn(tid)
    return True


# Alias for direct use: pass RedisCheckpointer(...) to create_agent(checkpointer=...)
RedisCheckpointer = RedisSaver



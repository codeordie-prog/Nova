"""
Redis checkpointer wrapper for agent state persistence.
Uses langgraph's built-in RedisSaver.
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


# Alias for direct use: pass RedisCheckpointer(...) to create_agent(checkpointer=...)
RedisCheckpointer = RedisSaver



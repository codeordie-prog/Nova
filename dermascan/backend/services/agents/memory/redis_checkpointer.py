"""
Redis checkpointer wrapper for agent state persistence.
Uses langgraph's built-in RedisSaver.
"""

from typing import Any, Dict

from langgraph.checkpoint.redis import RedisSaver
from redis.backoff import ExponentialBackoff
from redis.retry import Retry


def default_redis_connection_kwargs() -> Dict[str, Any]:
    """Connection options hardened for managed Redis (e.g. Redis Cloud).

    Managed providers close idle TCP connections from the server side. Without
    these settings, pooled sockets go stale silently and the next command fails
    with ``SERVER_CLOSED_CONNECTION_ERROR``. This combination keeps the link
    warm and recovers transparently when a single connection does drop.
    """
    return {
        "socket_keepalive": True,
        "socket_timeout": 10,
        "socket_connect_timeout": 10,
        "health_check_interval": 30,
        "retry": Retry(ExponentialBackoff(cap=10, base=1), retries=3),
    }


def context_from_url(url: str) -> Any:
    """Create and return a RedisSaver context manager from a connection URL."""
    return RedisSaver.from_conn_string(
        url,
        connection_args=default_redis_connection_kwargs(),
    )


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


RedisCheckpointer = RedisSaver

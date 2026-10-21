"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
License: MIT

Postgres connection pool for the ``billing`` schema.

Billing is the first subsystem in the repo that needs pooled, transactional
Postgres access. :mod:`dermascan.rules_engine` opens a fresh psycopg2
connection per query, which suits its read-only CIR lookups but not money
mutations that must run inside ``SELECT ... FOR UPDATE`` transactions.

Connection settings reuse the same ``POSTGRES_*`` variables as the rules
engine, including the legacy ``POSTGRESS_PASSWORD`` spelling that deployed
environments actually set. The resolver is duplicated here rather than
imported because importing the rules engine constructs a knowledge agent and
a vector store as a side effect.
"""


from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Final, Iterator, Optional, TypeVar

import psycopg
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

# Same precedence as dermascan/rules_engine/engine/rules_engine.py, where the
# legacy double-S spelling is what .env actually sets.
_POSTGRES_PASSWORD_ENV_KEYS: Final[tuple[str, ...]] = (
    "POSTGRESS_PASSWORD",
    "POSTGRES_PASSWORD",
    "CLOUD_POSTGRES_PASSWORD",
    "postgres_password",
)

_POOL_MIN_SIZE: Final[int] = 1
_POOL_MAX_SIZE: Final[int] = 5
_CONNECT_TIMEOUT_S: Final[int] = 15
# Recycle idle pool connections before Cloud SQL / proxy idle timeouts bite.
_POOL_MAX_IDLE_S: Final[float] = max(
    60.0, float(os.getenv("BILLING_DB_MAX_IDLE_S", "600"))
)
# TCP keepalives so idle Cloud Run instances (when CPU is allocated) stay visible.
_KEEPALIVES: Final[int] = 1
_KEEPALIVES_IDLE_S: Final[int] = max(
    1, int(os.getenv("BILLING_DB_KEEPALIVES_IDLE_S", "60"))
)
_KEEPALIVES_INTERVAL_S: Final[int] = max(
    1, int(os.getenv("BILLING_DB_KEEPALIVES_INTERVAL_S", "10"))
)
_KEEPALIVES_COUNT: Final[int] = max(
    1, int(os.getenv("BILLING_DB_KEEPALIVES_COUNT", "3"))
)

_POOL: Optional[ConnectionPool] = None


def _resolve_postgres_password() -> str:
    """
    Resolve the Postgres password from the supported variable names.

    Returns:
        str: Password string, or empty when none of the names are set.
    """
    for key in _POSTGRES_PASSWORD_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def build_conninfo() -> str:
    """
    Build a libpq connection string from the ``POSTGRES_*`` environment.

    Returns:
        str: Connection string suitable for psycopg.

    Raises:
        ValueError: When no password is configured.
    """
    password = _resolve_postgres_password()
    if not password:
        raise ValueError(
            "Postgres password is required. Set one of: "
            + ", ".join(_POSTGRES_PASSWORD_ENV_KEYS)
        )

    params: dict[str, Any] = {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": os.environ.get("POSTGRES_PORT", "5432"),
        "dbname": os.environ.get("POSTGRES_DATABASE", "qutis_db"),
        "user": os.environ.get("POSTGRES_USER", "postgres"),
        "password": password,
        "connect_timeout": _CONNECT_TIMEOUT_S,
    }
    sslmode = (
        os.environ.get("POSTGRES_SSLMODE", "").strip()
        or os.environ.get("CLOUD_POSTGRES_SSLMODE", "").strip()
    )
    if sslmode:
        params["sslmode"] = sslmode
    params["keepalives"] = _KEEPALIVES
    params["keepalives_idle"] = _KEEPALIVES_IDLE_S
    params["keepalives_interval"] = _KEEPALIVES_INTERVAL_S
    params["keepalives_count"] = _KEEPALIVES_COUNT
    return make_conninfo(**params)


def _check_pool_connection(conn: Connection) -> None:
    """
    Verify a pooled connection before handing it to billing code.

    psycopg_pool discards dead sockets when this raises.
    """
    conn.execute("SELECT 1")


def _is_transient_connection_error(exc: BaseException) -> bool:
    """
    Return True when a billing DB call likely failed due to a dropped socket.

    Args:
        exc: Exception from psycopg or the pool.

    Returns:
        bool: True for operational / connection-loss errors worth one retry.
    """
    if isinstance(exc, psycopg.OperationalError):
        return True
    message = str(exc).lower()
    needles = (
        "connection",
        "closed",
        "consuming input failed",
        "server closed",
        "broken pipe",
        "timeout",
    )
    return any(needle in message for needle in needles)


def get_pool(*, refresh: bool = False) -> ConnectionPool:
    """
    Return the cached billing connection pool, creating it on first use.

    Args:
        refresh: When True, close and replace an existing pool.

    Returns:
        ConnectionPool: Shared pool for the billing schema.
    """
    global _POOL
    if refresh:
        reset_pool()
    if _POOL is None:
        _POOL = ConnectionPool(
            conninfo=build_conninfo(),
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            max_idle=_POOL_MAX_IDLE_S,
            check=_check_pool_connection,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _POOL.open()
        logger.info(
            "billing db pool opened: max_size=%s max_idle_s=%s check=select_1",
            _POOL_MAX_SIZE,
            _POOL_MAX_IDLE_S,
        )
    return _POOL


def reset_pool() -> None:
    """Close and discard the cached pool. Used by tests and on reconfiguration."""
    global _POOL
    if _POOL is not None:
        try:
            _POOL.close()
        except Exception:
            logger.exception("billing db pool close failed")
        _POOL = None


_T = TypeVar("_T")


def run_transaction(fn: Callable[[Connection], _T]) -> _T:
    """
    Run ``fn`` inside :func:`transaction`, retrying once on transient connection loss.

    Use for short, idempotent billing mutations (e.g. ``get_or_create``) when a
    stale pooled socket would otherwise fail the whole HTTP request.

    Args:
        fn: Callable receiving the open connection inside a transaction.

    Returns:
        _T: Return value of ``fn``.

    Raises:
        psycopg.Error: When both attempts fail or the error is not transient.
    """
    last_exc: BaseException | None = None
    for attempt in (1, 2):
        try:
            with transaction() as conn:
                return fn(conn)
        except BaseException as exc:
            if attempt == 2 or not _is_transient_connection_error(exc):
                raise
            last_exc = exc
            logger.warning(
                "billing db transient connection error; retrying transaction once: %s",
                exc,
            )
    assert last_exc is not None
    raise last_exc


@contextmanager
def transaction() -> Iterator[Connection]:
    """
    Yield a pooled connection inside a transaction.

    Commits on clean exit, rolls back on exception. Every balance mutation in
    the billing module runs inside one of these.

    Yields:
        Connection: Pooled psycopg connection with ``dict_row`` rows.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def ping() -> bool:
    """
    Check that the billing database is reachable.

    Returns:
        bool: True when a trivial query succeeds.
    """
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        logger.exception("billing db ping failed")
        return False

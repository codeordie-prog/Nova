"""
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
from contextlib import contextmanager
from typing import Any, Final, Iterator, Optional

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
    return make_conninfo(**params)


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
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _POOL.open()
        logger.info("billing db pool opened: max_size=%s", _POOL_MAX_SIZE)
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

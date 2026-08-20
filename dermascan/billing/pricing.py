"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Reads and writes the versioned price list.

Prices live in ``billing.price_list`` rather than the environment so they can
be tuned from the admin portal without a deploy. Rows are inserted, never
updated: a charge from last week must still be explainable by the rate that
was in force when it ran, which is exactly what shadow mode is for.

Lookups are cached briefly because the deduction path reads rates once per
checkpoint, several times per job.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Final, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

SCOPE_JOB_CREDITS: Final[str] = "job_credits"
SCOPE_MODEL_TOKEN_RATE: Final[str] = "model_token_rate"
SCOPE_SETTING: Final[str] = "setting"

_CACHE_TTL_S: Final[float] = 60.0
_CACHE: dict[tuple[str, str], tuple[float, Decimal]] = {}


def clear_cache() -> None:
    """Drop the in-process price cache. Called after a write and by tests."""
    _CACHE.clear()


def _lookup(conn: Connection, scope: str, key: str) -> Optional[Decimal]:
    """
    Read the price in force right now.

    Args:
        conn: Open connection.
        scope: Price scope.
        key: Price key.

    Returns:
        Optional[Decimal]: Current value, or None when the key is unknown.
    """
    row = conn.execute(
        """
        SELECT value_numeric
        FROM billing.price_list
        WHERE scope = %s AND key = %s AND effective_from <= now()
        ORDER BY effective_from DESC
        LIMIT 1
        """,
        (scope, key),
    ).fetchone()
    return None if row is None else Decimal(row["value_numeric"])


def get_price(scope: str, key: str, *, default: Optional[Decimal] = None) -> Decimal:
    """
    Read a price, using the short-lived cache.

    Args:
        scope: One of the ``SCOPE_*`` constants.
        key: Price key, e.g. ``skin_audit``.
        default: Value to return when the key is not configured.

    Returns:
        Decimal: The configured value.

    Raises:
        KeyError: When the key is unknown and no default was given.
    """
    cache_key = (scope, key)
    cached = _CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]

    with transaction() as conn:
        value = _lookup(conn, scope, key)

    if value is None:
        if default is not None:
            return default
        raise KeyError(f"no price configured for {scope}/{key}")

    _CACHE[cache_key] = (now + _CACHE_TTL_S, value)
    return value


def job_credits(job_type: str) -> int:
    """
    Credit cost of one deliverable.

    Args:
        job_type: e.g. ``skin_audit``, ``product_review_image``.

    Returns:
        int: Whole credits to charge.
    """
    return int(get_price(SCOPE_JOB_CREDITS, job_type))


def setting(key: str, *, default: Optional[Decimal] = None) -> Decimal:
    """
    Read a scalar billing setting.

    Args:
        key: e.g. ``free_trial_credits``, ``min_topup_kes``.
        default: Value when unset.

    Returns:
        Decimal: The setting value.
    """
    return get_price(SCOPE_SETTING, key, default=default)


def free_trial_credits() -> int:
    """
    One-time credit grant for a new account.

    Returns:
        int: Credits granted on first contact.
    """
    return int(setting("free_trial_credits", default=Decimal(0)))


def min_topup_credits() -> int:
    """
    Smallest accepted top-up, in credits (1 credit = KES 1.00).

    Returns:
        int: Minimum top-up.
    """
    return int(setting("min_topup_kes", default=Decimal(100)))


def set_price(
    scope: str,
    key: str,
    value: Decimal,
    *,
    created_by: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """
    Record a new price, effective immediately.

    The previous row is left in place so historical charges stay explainable.

    Args:
        scope: One of the ``SCOPE_*`` constants.
        key: Price key.
        value: New value.
        created_by: Who changed it, for the audit trail.
        metadata: Optional extra context.
    """
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO billing.price_list (scope, key, value_numeric, created_by, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (scope, key, value, created_by, Jsonb(metadata or {})),
        )
    clear_cache()
    logger.info("price updated: scope=%s key=%s value=%s by=%s", scope, key, value, created_by)


def list_current_prices(scope: Optional[str] = None) -> list[dict[str, Any]]:
    """
    List the price in force for every key, for the admin portal.

    Args:
        scope: Restrict to one scope, or None for all.

    Returns:
        list[dict]: Rows of scope, key, value, effective_from.
    """
    sql = """
        SELECT DISTINCT ON (scope, key) scope, key, value_numeric, effective_from, created_by
        FROM billing.price_list
        WHERE effective_from <= now()
    """
    params: tuple[Any, ...] = ()
    if scope is not None:
        sql += " AND scope = %s"
        params = (scope,)
    sql += " ORDER BY scope, key, effective_from DESC"

    with transaction() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

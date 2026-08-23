"""
Author: Dermascan Team
Date: 2026-08-21
License: MIT

Feature flags for the meter/hold billing lane.

Paystack master switch (``BILLING_ENABLED``) lives on the colleague branch in
the same module after merge.
"""

from __future__ import annotations

import os
from typing import Final

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, *, default: bool = False) -> bool:
    """
    Parse a boolean environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        bool: Parsed flag value.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def billing_meter_enabled() -> bool:
    """
    Whether to record ``usage_line`` rows (shadow metering).

    Returns:
        bool: True when ``BILLING_METER_ENABLED`` is set.
    """
    return _env_flag("BILLING_METER_ENABLED", default=False)


def billing_enforce_enabled() -> bool:
    """
    Whether to place holds and block turns on insufficient credits.

    Returns:
        bool: True when ``BILLING_ENFORCE`` is set.
    """
    return _env_flag("BILLING_ENFORCE", default=False)


def billing_active() -> bool:
    """
    Whether any meter/hold lane feature is enabled.

    Returns:
        bool: True when metering or enforcement is on.
    """
    return billing_meter_enabled() or billing_enforce_enabled()


def validate_billing_flags() -> None:
    """
    Validate meter/hold billing flag combinations at worker startup.

    Raises:
        RuntimeError: When ``BILLING_ENFORCE`` is on without metering enabled.
    """
    if billing_enforce_enabled() and not billing_meter_enabled():
        raise RuntimeError(
            "BILLING_ENFORCE=true requires BILLING_METER_ENABLED=true so usage "
            "lines exist before settle."
        )

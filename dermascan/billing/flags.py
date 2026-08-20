"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Feature flags and credentials for the billing subsystem.

Deliberately minimal: only the switches this lane actually reads. The
charging-side knobs (enforcement, chat-turn metering, overdraft floor) land
with the per-checkpoint deduction work and are not declared here yet, so no
variable exists that nothing consumes.

Everything that is *tunable* rather than *operational* — credit prices, token
rates, top-up amounts, the free-trial grant — lives in ``billing.price_list``
and is edited from the admin portal, not from the environment.
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
        default: Value when the variable is unset or empty.

    Returns:
        bool: Parsed flag value.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def billing_enabled() -> bool:
    """
    Master switch for billing.

    When False, no account is created, no top-up is offered, and the webhook
    endpoint rejects traffic — behaviour is exactly as before billing existed.
    """
    return _env_flag("BILLING_ENABLED", default=False)


def paystack_secret_key() -> str:
    """
    Paystack secret key.

    Returns:
        str: ``sk_test_...`` or ``sk_live_...``, empty when unset.
    """
    return os.getenv("PAYSTACK_SECRET_KEY", "").strip()


def paystack_callback_url() -> str:
    """
    URL Paystack returns the user to after hosted checkout.

    Returns:
        str: Absolute URL, empty when unset.
    """
    return os.getenv("PAYSTACK_CALLBACK_URL", "").strip()


def paystack_is_live() -> bool:
    """
    Whether the configured Paystack key charges real money.

    Returns:
        bool: True for a live secret key.
    """
    return paystack_secret_key().startswith("sk_live_")

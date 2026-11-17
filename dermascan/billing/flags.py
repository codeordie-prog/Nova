"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
Licence: PolyForm Noncommercial License 1.0.0

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


def billing_meter_enabled() -> bool:
    """
    Whether to record ``usage_line`` rows.

    Metering is safe to run on its own: usage lines are balance-neutral until a
    reservation settles, so this is how the hold ceilings get sized from real
    traffic before anything is enforced.

    Returns:
        bool: True when ``BILLING_METER_ENABLED`` is set.
    """
    return _env_flag("BILLING_METER_ENABLED", default=False)


def billing_enforce_enabled() -> bool:
    """
    Whether to place holds and turn a request away on insufficient credits.

    Returns:
        bool: True when ``BILLING_ENFORCE`` is set.
    """
    return _env_flag("BILLING_ENFORCE", default=False)


def billing_active() -> bool:
    """
    Whether any metering or enforcement path is live.

    Returns:
        bool: True when metering or enforcement is on.
    """
    return billing_meter_enabled() or billing_enforce_enabled()


def validate_billing_flags() -> None:
    """
    Check flag combinations at worker startup.

    Raises:
        RuntimeError: When enforcement is on without metering, which would
            settle every reservation against zero usage lines and refund the
            whole hold.
    """
    if billing_enforce_enabled() and not billing_meter_enabled():
        raise RuntimeError(
            "BILLING_ENFORCE=true requires BILLING_METER_ENABLED=true so usage "
            "lines exist before settle."
        )

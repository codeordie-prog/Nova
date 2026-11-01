"""Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Feature flags for the reminders subsystem."""

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


def reminders_enabled() -> bool:
    """
    Master switch for reminder workers and tool side effects.

    When False, tools may still return guidance but must not mutate Postgres.
    """
    return _env_flag("REMINDERS_ENABLED", default=False)


def default_timezone() -> str:
    """
    Default IANA timezone for new reminder state rows.

    Returns:
        str: Timezone name, overridable via ``REMINDERS_DEFAULT_TIMEZONE``.
    """
    return os.getenv("REMINDERS_DEFAULT_TIMEZONE", "Africa/Nairobi").strip() or "Africa/Nairobi"

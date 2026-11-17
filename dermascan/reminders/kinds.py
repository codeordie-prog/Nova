"""
Author: Dermascan Team
Date: 11-1-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Reminder kind allowlist for v1.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

ReminderKind = Literal["morning_briefing", "routine_reminder"]

ALL_REMINDER_KINDS: Final[tuple[ReminderKind, ...]] = (
    "morning_briefing",
    "routine_reminder",
)

MORNING_BRIEF_LOCAL_TIME: Final[str] = "07:00"


def normalize_kind(raw: str) -> ReminderKind:
    """
    Validate a reminder kind string.

    Args:
        raw: Kind from DB or API.

    Returns:
        ReminderKind: Valid kind.

    Raises:
        ValueError: When kind is not in the allowlist.
    """
    value = (raw or "").strip().lower()
    if value not in get_args(ReminderKind):
        allowed = ", ".join(ALL_REMINDER_KINDS)
        raise ValueError(f"unsupported reminder kind {raw!r}; expected one of: {allowed}")
    return value  # type: ignore[return-value]

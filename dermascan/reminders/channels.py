"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Channel identifiers for reminders (shared with billing ingress).

"""

from __future__ import annotations

from typing import Final, Literal, get_args

ReminderChannel = Literal["whatsapp", "telegram"]

ALL_REMINDER_CHANNELS: Final[tuple[ReminderChannel, ...]] = ("whatsapp", "telegram")

# v1 dispatch and orchestrator tools are wired for WhatsApp only; Telegram uses the same schema.
V1_ACTIVE_CHANNELS: Final[frozenset[ReminderChannel]] = frozenset({"whatsapp"})

DEFAULT_V1_CHANNEL: Final[ReminderChannel] = "whatsapp"


def normalize_channel(raw: str) -> ReminderChannel:
    """
    Normalize and validate a channel string.

    Args:
        raw: Channel from ingress or tool input.

    Returns:
        ReminderChannel: Lowercased allowed channel.

    Raises:
        ValueError: When the channel is not supported in the schema.
    """
    value = (raw or "").strip().lower()
    if value not in get_args(ReminderChannel):
        allowed = ", ".join(ALL_REMINDER_CHANNELS)
        raise ValueError(f"unsupported channel {raw!r}; expected one of: {allowed}")
    return value  # type: ignore[return-value]


def channel_active_in_v1(channel: str) -> bool:
    """
    Return True when reminder features should run for this channel in v1.

    Args:
        channel: Raw or normalized channel name.

    Returns:
        bool: True for WhatsApp; False for Telegram until explicitly enabled.
    """
    try:
        normalized = normalize_channel(channel)
    except ValueError:
        return False
    return normalized in V1_ACTIVE_CHANNELS

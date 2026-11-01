"""Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

User reminders (morning brief + routines); WhatsApp-first, channel-extensible."""

from dermascan.reminders.channels import (
    ALL_REMINDER_CHANNELS,
    DEFAULT_V1_CHANNEL,
    V1_ACTIVE_CHANNELS,
    channel_active_in_v1,
    normalize_channel,
)
from dermascan.reminders.flags import default_timezone, reminders_enabled

__all__ = [
    "ALL_REMINDER_CHANNELS",
    "DEFAULT_V1_CHANNEL",
    "V1_ACTIVE_CHANNELS",
    "channel_active_in_v1",
    "default_timezone",
    "normalize_channel",
    "reminders_enabled",
]

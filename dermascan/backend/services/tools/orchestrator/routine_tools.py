"""
Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Orchestrator tools for morning briefings and routine reminders (WhatsApp v1).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Optional
from uuid import UUID

from langchain_core.tools import tool
from pydantic import Field

from dermascan.billing import accounts as billing_accounts
from dermascan.reminders.channels import channel_active_in_v1, normalize_channel
from dermascan.reminders.flags import default_timezone, reminders_enabled
from dermascan.reminders.repository import (
    disable_reminder,
    list_reminders,
    upsert_morning_briefing,
    upsert_routine_reminder,
)
from dermascan.reminders.routine_key import routine_key_from_label
from dermascan.reminders.scheduling import parse_schedule
from dermascan.shared.escalation_image import INGRESS_PAYLOAD_CACHE_PREFIX
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client

logger = get_logger(__name__)

ReminderClientIdArg = Annotated[
    str,
    Field(
        description=(
            "Required. Exact client_id from the orchestrator input "
            "(copy the value shown after 'client_id:'). "
            "Do not use request_id or case_id."
        ),
    ),
]

ReminderRequestIdArg = Annotated[
    str,
    Field(
        description=(
            "Required. Exact request_id from the current turn "
            "(copy the value shown after 'request_id:')."
        ),
    ),
]

RoutineLabelArg = Annotated[
    str,
    Field(description="Short user-facing name for the habit (e.g. Sunscreen reapply)."),
]

EveryHoursArg = Annotated[
    Optional[int],
    Field(
        default=None,
        description="Repeat every N hours (1–168). Use for habits like sunscreen every 4 hours.",
    ),
]

DailyLocalTimeArg = Annotated[
    Optional[str],
    Field(
        default=None,
        description='Optional daily time in 24h HH:MM user local time (e.g. "21:30" for evening retinol).',
    ),
]

TimezoneArg = Annotated[
    Optional[str],
    Field(
        default=None,
        description=(
            "Optional IANA timezone (e.g. Africa/Nairobi). "
            "Omit to keep the user's stored timezone or the system default."
        ),
    ),
]

RoutineKeyArg = Annotated[
    Optional[str],
    Field(
        default=None,
        description=(
            "Optional stable slot key. Omit on create to derive from label; "
            "required when updating an existing routine if list_routines gave you routine_key."
        ),
    ),
]

ReminderIdArg = Annotated[
    Optional[str],
    Field(
        default=None,
        description="UUID from list_routines when disabling or confirming a specific row.",
    ),
]


def _disabled_payload() -> dict[str, Any]:
    """
    Build the agent-facing response when ``REMINDERS_ENABLED`` is off.

    Returns:
        dict[str, Any]: Status ``reminders_disabled`` plus an ``instruction`` for the model.
    """
    return {
        "status": "reminders_disabled",
        "instruction": (
            "Personal reminders are not enabled in this environment. "
            "Apologise briefly and continue helping without promising scheduled pings."
        ),
    }


def _unsupported_channel_payload(channel: str) -> dict[str, Any]:
    """
    Build the agent-facing response when the channel is not active in reminders v1.

    Args:
        channel: Normalized channel name (e.g. ``telegram``).

    Returns:
        dict[str, Any]: Status ``unsupported_channel`` plus guidance for the model.
    """
    return {
        "status": "unsupported_channel",
        "channel": channel,
        "instruction": (
            "Scheduled reminders are available on WhatsApp only for now. "
            "Tell the user gently and offer to note preferences in chat."
        ),
    }


def _ingress_envelope(request_id: str) -> dict[str, str]:
    """
    Load ``client_id`` and ``channel`` from the orchestrator ingress Redis cache.

    Args:
        request_id: Current turn request id.

    Returns:
        dict[str, str]: Subset of envelope fields; empty dict when cache is missing or invalid.
    """
    rid = str(request_id or "").strip()
    if not rid:
        return {}
    try:
        redis_client = get_redis_client(decode_responses=True)
        raw = redis_client.get(f"{INGRESS_PAYLOAD_CACHE_PREFIX}:{rid}")
    except Exception:
        logger.exception("reminder tool ingress cache read failed: request_id=%s", rid)
        return {}
    if not raw:
        return {}
    try:
        cached = json.loads(str(raw))
    except json.JSONDecodeError:
        logger.exception("reminder tool ingress cache malformed: request_id=%s", rid)
        return {}
    if not isinstance(cached, dict):
        return {}
    if "payload" in cached:
        return {
            "request_id": str(cached.get("request_id") or rid),
            "client_id": str(cached.get("client_id") or ""),
            "channel": str(cached.get("channel") or ""),
        }
    return {"request_id": rid, "client_id": "", "channel": "whatsapp"}


def _resolve_channel(client_id: str, request_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    Resolve and validate the channel for a reminder tool call.

    Args:
        client_id: User id from the orchestrator turn.
        request_id: Current turn request id (ingress cache).

    Returns:
        tuple[str | None, dict[str, Any] | None]: ``(channel, None)`` on success, or
        ``(None, error_dict)`` when reminders are disabled, the channel is unsupported,
        or ``client_id`` disagrees with the cached envelope.
    """
    if not reminders_enabled():
        return None, _disabled_payload()

    envelope = _ingress_envelope(request_id)
    channel_raw = str(envelope.get("channel") or "").strip()
    if not channel_raw:
        channel_raw = str(billing_accounts.resolve_channel(client_id) or "").strip()
    if not channel_raw:
        channel_raw = "whatsapp"

    try:
        channel = normalize_channel(channel_raw)
    except ValueError:
        return None, {
            "status": "error",
            "detail": f"unknown channel {channel_raw!r}",
            "instruction": "Could not configure reminders for this chat. Apologise and continue.",
        }

    if not channel_active_in_v1(channel):
        return None, _unsupported_channel_payload(channel)

    env_client = str(envelope.get("client_id") or "").strip()
    if env_client and env_client != client_id.strip():
        return None, {
            "status": "error",
            "detail": "client_id does not match ingress envelope for request_id",
            "instruction": "Use the client_id from the current turn input, then retry once.",
        }
    return channel, None


def _schedule_from_args(
    *,
    every_hours: int | None,
    daily_local_time: str | None,
) -> dict[str, Any]:
    """
    Build a validated schedule document from tool arguments.

    Args:
        every_hours: Interval schedule (mutually exclusive with ``daily_local_time``).
        daily_local_time: Daily HH:MM local time schedule.

    Returns:
        dict[str, Any]: Valid schedule spec for Postgres, or an error dict with
        ``status`` ``invalid_schedule`` when arguments are missing or invalid.
    """
    if every_hours is not None and daily_local_time:
        return {
            "status": "invalid_schedule",
            "instruction": (
                "Pick either an interval (every N hours) or a daily time, not both. "
                "Ask the user which they prefer."
            ),
        }
    if every_hours is not None:
        spec = {"type": "interval_hours", "every_hours": every_hours}
    elif daily_local_time:
        spec = {"type": "daily_time", "local_time": daily_local_time.strip()}
    else:
        return {
            "status": "invalid_schedule",
            "instruction": (
                "Ask how often to remind them: every few hours, or a specific daily time."
            ),
        }
    try:
        parse_schedule(spec)
    except ValueError as exc:
        return {
            "status": "invalid_schedule",
            "detail": str(exc),
            "instruction": f"Fix the schedule: {exc}",
        }
    return spec  # type: ignore[return-value]


def _serialize_reminder_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Postgres reminder row into JSON-safe tool output.

    Args:
        row: Dict row from ``reminders.reminder`` (may contain UUID/datetime values).

    Returns:
        dict[str, Any]: Same keys with datetimes ISO-formatted and UUIDs as strings.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out


@tool
def set_morning_brief(
    client_id: ReminderClientIdArg,
    request_id: ReminderRequestIdArg,
    enabled: bool,
    timezone_name: TimezoneArg = None,
) -> dict[str, Any]:
    """
    Enable or disable the daily morning briefing (~07:00 local, UV/skincare context).

    Args:
        client_id: Exact client_id from the orchestrator turn.
        request_id: Exact request_id from the orchestrator turn.
        enabled: True to opt in, False to turn off.
        timezone_name: Optional IANA timezone; uses stored or default when omitted.

    Returns:
        dict[str, Any]: ``status`` ok/error/disabled/unsupported_channel plus ``instruction``
        for the model and ``reminder`` row on success. Does not raise; DB errors become
        ``status`` ``error``.
    """
    logger.info(
        "TOOL CALL: set_morning_brief client_id=%s enabled=%s",
        client_id,
        enabled,
    )
    channel, err = _resolve_channel(client_id, request_id)
    if err:
        return err
    assert channel is not None
    try:
        row = upsert_morning_briefing(
            channel,
            client_id,
            enabled=enabled,
            timezone_name=timezone_name or default_timezone(),
        )
    except Exception:
        logger.exception("set_morning_brief failed: client_id=%s", client_id)
        return {
            "status": "error",
            "instruction": (
                "Could not save the morning brief setting. Apologise and suggest trying again shortly."
            ),
        }
    return {
        "status": "ok",
        "reminder": _serialize_reminder_row(row),
        "instruction": (
            "Confirm whether the morning brief is on or off. "
            "Mention it sends around 7am their time when they have chatted recently."
        ),
    }


@tool
def set_routine_reminder(
    client_id: ReminderClientIdArg,
    request_id: ReminderRequestIdArg,
    label: RoutineLabelArg,
    every_hours: EveryHoursArg = None,
    daily_local_time: DailyLocalTimeArg = None,
    routine_key: RoutineKeyArg = None,
    enabled: bool = True,
    timezone_name: TimezoneArg = None,
) -> dict[str, Any]:
    """
    Create or update a skincare routine reminder (upsert by ``routine_key``).

    Args:
        client_id: Exact client_id from the orchestrator turn.
        request_id: Exact request_id from the orchestrator turn.
        label: User-facing habit name.
        every_hours: Repeat every N hours (1–168), or omit when using ``daily_local_time``.
        daily_local_time: Daily fire time HH:MM local, or omit when using ``every_hours``.
        routine_key: Optional stable slot; derived from ``label`` when omitted on create.
        enabled: Whether the slot is active.
        timezone_name: Optional IANA timezone for daily schedules.

    Returns:
        dict[str, Any]: Tool status payload with ``reminder`` and ``routine_key`` on success.
        Does not raise; validation and DB failures are returned as status dicts.
    """
    logger.info("TOOL CALL: set_routine_reminder client_id=%s label=%s", client_id, label)
    channel, err = _resolve_channel(client_id, request_id)
    if err:
        return err
    assert channel is not None

    schedule_result = _schedule_from_args(
        every_hours=every_hours,
        daily_local_time=daily_local_time,
    )
    if isinstance(schedule_result, dict) and schedule_result.get("status"):
        return schedule_result
    schedule = schedule_result

    key = (routine_key or "").strip()
    if not key:
        try:
            key = routine_key_from_label(label)
        except ValueError as exc:
            return {
                "status": "invalid_routine_key",
                "detail": str(exc),
                "instruction": "Ask for a shorter habit name.",
            }

    try:
        row = upsert_routine_reminder(
            channel,
            client_id,
            routine_key=key,
            label=label.strip(),
            schedule=schedule,
            enabled=enabled,
            timezone_name=timezone_name,
        )
    except Exception:
        logger.exception("set_routine_reminder failed: client_id=%s key=%s", client_id, key)
        return {
            "status": "error",
            "instruction": "Could not save that reminder. Apologise and offer to try again.",
        }
    return {
        "status": "ok",
        "routine_key": key,
        "reminder": _serialize_reminder_row(row),
        "instruction": (
            "Confirm the habit and schedule in plain language. "
            "Reminders only send while they have messaged on WhatsApp in the last day."
        ),
    }


@tool
def list_routines(
    client_id: ReminderClientIdArg,
    request_id: ReminderRequestIdArg,
) -> dict[str, Any]:
    """
    List morning briefing and routine reminder rows for this user.

    Args:
        client_id: Exact client_id from the orchestrator turn.
        request_id: Exact request_id from the orchestrator turn.

    Returns:
        dict[str, Any]: ``status`` ok with ``reminders`` list, or error/disabled payloads.
        Does not raise.
    """
    logger.info("TOOL CALL: list_routines client_id=%s", client_id)
    channel, err = _resolve_channel(client_id, request_id)
    if err:
        return err
    assert channel is not None
    try:
        rows = list_reminders(channel, client_id)
    except Exception:
        logger.exception("list_routines failed: client_id=%s", client_id)
        return {
            "status": "error",
            "instruction": "Could not load reminders. Apologise and continue the conversation.",
        }
    serialized = [_serialize_reminder_row(r) for r in rows]
    return {
        "status": "ok",
        "reminders": serialized,
        "instruction": (
            "Summarise active routines briefly. "
            "To change one, call set_routine_reminder with the same routine_key or disable_routine."
        ),
    }


@tool
def disable_routine(
    client_id: ReminderClientIdArg,
    request_id: ReminderRequestIdArg,
    reminder_id: ReminderIdArg = None,
    routine_key: RoutineKeyArg = None,
) -> dict[str, Any]:
    """
    Disable a reminder row (routine or morning brief via ``reminder_id``).

    Args:
        client_id: Exact client_id from the orchestrator turn.
        request_id: Exact request_id from the orchestrator turn.
        reminder_id: UUID from ``list_routines`` (preferred for morning brief).
        routine_key: Slot key from ``list_routines`` for routine rows.

    Returns:
        dict[str, Any]: ``status`` ok, not_found, invalid_args, or error. Does not raise.
    """
    logger.info(
        "TOOL CALL: disable_routine client_id=%s reminder_id=%s routine_key=%s",
        client_id,
        reminder_id,
        routine_key,
    )
    channel, err = _resolve_channel(client_id, request_id)
    if err:
        return err
    assert channel is not None

    rid: UUID | None = None
    if reminder_id and str(reminder_id).strip():
        try:
            rid = UUID(str(reminder_id).strip())
        except ValueError:
            return {
                "status": "invalid_reminder_id",
                "instruction": "Call list_routines and use the exact reminder_id UUID.",
            }

    if rid is None and not (routine_key and routine_key.strip()):
        return {
            "status": "invalid_args",
            "instruction": "Call list_routines first, then pass reminder_id or routine_key.",
        }

    try:
        updated = disable_reminder(
            channel,
            client_id,
            reminder_id=rid,
            routine_key=routine_key,
        )
    except Exception:
        logger.exception("disable_routine failed: client_id=%s", client_id)
        return {
            "status": "error",
            "instruction": "Could not disable that reminder. Apologise and offer to try again.",
        }
    if not updated:
        return {
            "status": "not_found",
            "instruction": "No matching reminder was found. Use list_routines to verify.",
        }
    return {
        "status": "ok",
        "instruction": "Confirm the reminder is turned off.",
    }

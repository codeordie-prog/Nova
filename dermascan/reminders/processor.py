"""Author: Dermascan Team
Date: 11-1-2026
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Process due reminder rows (window gate, pause, outbound/signal enqueue)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from dermascan.reminders.morning_brief import build_morning_brief_text
from dermascan.reminders.outbound import (
    enqueue_morning_brief_signal,
    enqueue_routine_reminder_text,
)
from dermascan.reminders.repository import apply_fire_outcome
from dermascan.reminders.scheduling import compute_next_fire_at
from dermascan.reminders.window import in_service_window
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


def _utc_now() -> datetime:
    """
    Current UTC time for fire processing.

    Returns:
        datetime: Timezone-aware UTC instant.
    """
    return datetime.now(timezone.utc)


def _local_today(timezone_name: str, now: datetime) -> date:
    """
    Calendar date in the user's timezone.

    Args:
        timezone_name: IANA timezone name.
        now: Reference instant (UTC).

    Returns:
        date: Local calendar date (falls back to UTC on invalid TZ).
    """
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(timezone_name)).date()
    except Exception:
        return now.date()


def routine_reminder_text(row: dict[str, Any]) -> str:
    """
    Build deterministic outbound copy for a routine reminder.

    Args:
        row: Due reminder join row from ``list_due_reminders``.

    Returns:
        str: User-facing reminder text.
    """
    label = str(row.get("label") or "").strip()
    key = str(row.get("routine_key") or "").strip()
    habit = label or key.replace("_", " ") or "your skincare habit"
    return (
        f"Reminder: {habit}. "
        "Small consistent steps add up — reply if you want tips or to adjust this reminder."
    )


def process_due_reminder_row(redis_client: Any, row: dict[str, Any]) -> str:
    """
    Evaluate one due reminder and enqueue delivery or record a skip outcome.

    Args:
        redis_client: Decode-responses Redis client for stream writes.
        row: Due row from ``list_due_reminders`` (includes user state columns).

    Returns:
        str: Outcome token matching ``fire_log.outcome`` (e.g. ``sent``, ``skipped_paused``).

    Raises:
        RuntimeError: When enqueue fails after DB state would be inconsistent (logged upstream).
    """
    now = _utc_now()
    reminder_id = row["id"]
    if not isinstance(reminder_id, UUID):
        reminder_id = UUID(str(reminder_id))
    channel = str(row.get("channel") or "")
    client_id = str(row.get("client_id") or "")
    kind = str(row.get("kind") or "")
    tz = str(row.get("timezone") or "Africa/Nairobi")
    schedule = row.get("schedule") if isinstance(row.get("schedule"), dict) else {}
    paused = bool(row.get("reminders_paused"))
    last_inbound = row.get("last_inbound_at")

    next_fire = compute_next_fire_at(
        schedule,
        timezone_name=tz,
        after=now,
        last_fired_at=now,
    )

    def _finalize(outcome: str, detail: dict[str, Any], **pending: Any) -> str:
        apply_fire_outcome(
            reminder_id,
            channel=channel,
            client_id=client_id,
            kind=kind,
            outcome=outcome,
            detail=detail,
            fired_at=now,
            next_fire_at=next_fire,
            **pending,
        )
        return outcome

    if paused:
        logger.info(
            "reminder skipped paused: client_id=%s kind=%s id=%s",
            client_id,
            kind,
            reminder_id,
        )
        return _finalize("skipped_paused", {"reason": "reminders_paused"})

    if not in_service_window(last_inbound, now):
        if kind == "morning_briefing":
            pending_day = _local_today(tz, now)
            draft = build_morning_brief_text(timezone_name=tz)
            return _finalize(
                "skipped_outside_window",
                {"reason": "outside_24h_window", "pending": True},
                morning_pending_on=pending_day,
                morning_pending_payload={"draft": draft, "timezone": tz},
            )
        return _finalize(
            "skipped_outside_window",
            {"reason": "outside_24h_window"},
        )

    if kind == "routine_reminder":
        text = routine_reminder_text(row)
        enqueue_routine_reminder_text(
            redis_client,
            client_id=client_id,
            channel=channel,
            text=text,
            reminder_id=str(reminder_id),
        )
        return _finalize("sent", {"delivery": "outbound", "kind": "routine_reminder"})

    if kind == "morning_briefing":
        enqueue_morning_brief_signal(
            redis_client,
            client_id=client_id,
            channel=channel,
            reminder_id=str(reminder_id),
            timezone_name=tz,
        )
        return _finalize(
            "sent",
            {"delivery": "orchestrator_signal"},
            clear_morning_pending=True,
        )

    return _finalize("failed", {"reason": f"unknown kind {kind!r}"})

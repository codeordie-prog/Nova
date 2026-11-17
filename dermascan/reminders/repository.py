"""
Author: Dermascan Team
Date: 11-1-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Postgres persistence for reminders (``reminders`` schema).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any, TypeVar
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from dermascan.billing.db import run_read, run_transaction
from dermascan.reminders.channels import normalize_channel
from dermascan.reminders.flags import default_timezone
from dermascan.reminders.scheduling import compute_next_fire_at, morning_brief_schedule


_T = TypeVar("_T")


def _utc_now() -> datetime:
    """
    Current UTC time for persistence and scheduling.

    Returns:
        datetime: Timezone-aware UTC instant.
    """
    return datetime.now(timezone.utc)


def _run_read(fn: Callable[[Connection], _T]) -> _T:
    """
    Execute a read callback with one retry on transient Postgres connection loss.

    Args:
        fn: Query callable receiving a pooled connection.

    Returns:
        _T: Result of ``fn``.

    Raises:
        psycopg.Error: When the read fails after retry.
    """
    return run_read(fn)


def _run_write(fn: Callable[[Connection], _T]) -> _T:
    """
    Execute a write or transactional callback with one retry on transient connection loss.

    Args:
        fn: Mutation callable receiving a connection inside a transaction.

    Returns:
        _T: Result of ``fn``.

    Raises:
        psycopg.Error: When the transaction fails after retry.
    """
    return run_transaction(fn)


def touch_last_inbound(
    channel: str,
    client_id: str,
    at: datetime | None = None,
) -> None:
    """
    Upsert user state and set ``last_inbound_at`` (opens/refreshes the 24h window).

    Args:
        channel: ``whatsapp`` or ``telegram``.
        client_id: Stable channel user id.
        at: Inbound timestamp; defaults to now UTC.

    Raises:
        ValueError: When ``client_id`` is empty or ``channel`` is invalid.
        psycopg.Error: When the database write fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()
    if not cid:
        raise ValueError("client_id is required")
    inbound = at if at is not None else _utc_now()
    if inbound.tzinfo is None:
        inbound = inbound.replace(tzinfo=timezone.utc)

    def _write(conn: Connection) -> None:
        conn.execute(
            """
            INSERT INTO reminders.user_channel_state (
                channel, client_id, last_inbound_at, timezone, updated_at
            )
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (channel, client_id) DO UPDATE SET
                last_inbound_at = EXCLUDED.last_inbound_at,
                updated_at = now()
            """,
            (ch, cid, inbound, default_timezone()),
        )

    _run_write(_write)


def set_reminders_paused(channel: str, client_id: str, paused: bool) -> None:
    """
    Set whether dispatch should skip this user (e.g. MYDAWA handoff).

    Args:
        channel: Channel name.
        client_id: User id on that channel.
        paused: True to pause proactive reminders.

    Raises:
        ValueError: When ``client_id`` is empty or ``channel`` is invalid.
        psycopg.Error: When the database write fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()
    if not cid:
        raise ValueError("client_id is required")

    def _write(conn: Connection) -> None:
        conn.execute(
            """
            INSERT INTO reminders.user_channel_state (
                channel, client_id, reminders_paused, timezone, updated_at
            )
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (channel, client_id) DO UPDATE SET
                reminders_paused = EXCLUDED.reminders_paused,
                updated_at = now()
            """,
            (ch, cid, paused, default_timezone()),
        )

    _run_write(_write)


def get_user_state(channel: str, client_id: str) -> dict[str, Any] | None:
    """
    Load user channel state row.

    Args:
        channel: Channel name.
        client_id: User id.

    Returns:
        dict | None: Row as dict, or None when missing.

    Raises:
        ValueError: When ``channel`` is invalid.
        psycopg.Error: When the database read fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()

    def _read(conn: Connection) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT channel, client_id, last_inbound_at, timezone,
                   reminders_paused, morning_pending_on, morning_pending_payload,
                   created_at, updated_at
            FROM reminders.user_channel_state
            WHERE channel = %s AND client_id = %s
            """,
            (ch, cid),
        ).fetchone()
        return dict(row) if row else None

    return _run_read(_read)


def upsert_morning_briefing(
    channel: str,
    client_id: str,
    *,
    enabled: bool,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """
    Create or update the singleton morning briefing row for a user.

    Args:
        channel: Channel name.
        client_id: User id.
        enabled: Whether the brief is active.
        timezone_name: IANA TZ; defaults to env default when creating state.

    Returns:
        dict: Reminder row fields including ``id`` and ``next_fire_at``.

    Raises:
        ValueError: When ``client_id`` is empty, ``channel`` is invalid, or scheduling fails.
        RuntimeError: When the upsert returns no row.
        psycopg.Error: When the database transaction fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()
    if not cid:
        raise ValueError("client_id is required")
    tz = (timezone_name or default_timezone()).strip()
    schedule = morning_brief_schedule()
    now = _utc_now()

    def _write(conn: Connection) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO reminders.user_channel_state (channel, client_id, timezone, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (channel, client_id) DO UPDATE SET
                timezone = COALESCE(NULLIF(EXCLUDED.timezone, ''), reminders.user_channel_state.timezone),
                updated_at = now()
            """,
            (ch, cid, tz),
        )
        next_fire: datetime | None = None
        if enabled:
            next_fire = compute_next_fire_at(
                schedule,
                timezone_name=tz,
                after=now,
            )
        row = conn.execute(
            """
            INSERT INTO reminders.reminder (
                channel, client_id, kind, schedule, enabled, next_fire_at, updated_at
            )
            VALUES (%s, %s, 'morning_briefing', %s, %s, %s, now())
            ON CONFLICT (channel, client_id) WHERE (kind = 'morning_briefing') DO UPDATE SET
                schedule = EXCLUDED.schedule,
                enabled = EXCLUDED.enabled,
                next_fire_at = EXCLUDED.next_fire_at,
                updated_at = now()
            RETURNING id, channel, client_id, kind, routine_key, label, schedule,
                      enabled, next_fire_at, last_fired_at, metadata, created_at, updated_at
            """,
            (ch, cid, Jsonb(schedule), enabled, next_fire),
        ).fetchone()
        if row is None:
            raise RuntimeError("morning briefing upsert returned no row")
        return dict(row)

    return _run_write(_write)


def upsert_routine_reminder(
    channel: str,
    client_id: str,
    *,
    routine_key: str,
    label: str,
    schedule: dict[str, Any],
    enabled: bool = True,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """
    Create or update a routine reminder slot (unique per ``routine_key``).

    Args:
        channel: Channel name.
        client_id: User id.
        routine_key: Stable slot key.
        label: User-facing habit name.
        schedule: Valid schedule JSON.
        enabled: Active flag.
        timezone_name: IANA TZ for daily_time schedules.

    Returns:
        dict: Reminder row.

    Raises:
        ValueError: When ids/keys are missing, ``channel`` is invalid, or schedule is invalid.
        RuntimeError: When the upsert returns no row.
        psycopg.Error: When the database transaction fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()
    key = routine_key.strip()
    if not cid or not key:
        raise ValueError("client_id and routine_key are required")
    tz = (timezone_name or default_timezone()).strip()
    now = _utc_now()
    next_fire: datetime | None = None
    if enabled:
        next_fire = compute_next_fire_at(schedule, timezone_name=tz, after=now)

    def _write(conn: Connection) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO reminders.user_channel_state (channel, client_id, timezone, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (channel, client_id) DO NOTHING
            """,
            (ch, cid, tz),
        )
        row = conn.execute(
            """
            INSERT INTO reminders.reminder (
                channel, client_id, kind, routine_key, label, schedule,
                enabled, next_fire_at, updated_at
            )
            VALUES (%s, %s, 'routine_reminder', %s, %s, %s, %s, %s, now())
            ON CONFLICT (channel, client_id, routine_key)
            WHERE kind = 'routine_reminder'
            DO UPDATE SET
                label = EXCLUDED.label,
                schedule = EXCLUDED.schedule,
                enabled = EXCLUDED.enabled,
                next_fire_at = EXCLUDED.next_fire_at,
                updated_at = now()
            RETURNING id, channel, client_id, kind, routine_key, label, schedule,
                      enabled, next_fire_at, last_fired_at, metadata, created_at, updated_at
            """,
            (ch, cid, key, label.strip() or key, Jsonb(schedule), enabled, next_fire),
        ).fetchone()
        if row is None:
            raise RuntimeError("routine reminder upsert returned no row")
        return dict(row)

    return _run_write(_write)


def list_reminders(channel: str, client_id: str) -> list[dict[str, Any]]:
    """
    List all reminder rows for a user on a channel.

    Args:
        channel: Channel name.
        client_id: User id.

    Returns:
        list: Reminder dicts ordered by kind then routine_key.

    Raises:
        ValueError: When ``channel`` is invalid.
        psycopg.Error: When the database read fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()

    def _read(conn: Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, channel, client_id, kind, routine_key, label, schedule,
                   enabled, next_fire_at, last_fired_at, metadata, created_at, updated_at
            FROM reminders.reminder
            WHERE channel = %s AND client_id = %s
            ORDER BY kind, routine_key NULLS FIRST
            """,
            (ch, cid),
        ).fetchall()
        return [dict(r) for r in rows]

    return _run_read(_read)


def disable_reminder(
    channel: str,
    client_id: str,
    *,
    reminder_id: UUID | None = None,
    routine_key: str | None = None,
) -> bool:
    """
    Disable a reminder by id or routine_key.

    Args:
        channel: Channel name.
        client_id: User id.
        reminder_id: Row uuid.
        routine_key: Routine slot key (routine kind only).

    Returns:
        bool: True when a row was updated.

    Raises:
        ValueError: When neither selector is provided or ``channel`` is invalid.
        psycopg.Error: When the database write fails after retry.
    """
    if reminder_id is None and not (routine_key and routine_key.strip()):
        raise ValueError("reminder_id or routine_key is required")
    ch = normalize_channel(channel)
    cid = client_id.strip()

    def _write(conn: Connection) -> bool:
        if reminder_id is not None:
            cur = conn.execute(
                """
                UPDATE reminders.reminder
                SET enabled = false, next_fire_at = NULL, updated_at = now()
                WHERE channel = %s AND client_id = %s AND id = %s
                """,
                (ch, cid, reminder_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE reminders.reminder
                SET enabled = false, next_fire_at = NULL, updated_at = now()
                WHERE channel = %s AND client_id = %s
                  AND kind = 'routine_reminder' AND routine_key = %s
                """,
                (ch, cid, routine_key.strip()),
            )
        return cur.rowcount > 0

    return _run_write(_write)


def list_due_reminders(limit: int = 100) -> list[dict[str, Any]]:
    """
    Fetch enabled reminders due for dispatch (scheduler tick).

    Args:
        limit: Max rows per tick.

    Returns:
        list: Due reminder rows with user state joined (empty when ``limit`` < 1).

    Raises:
        psycopg.Error: When the database transaction fails after retry.
    """
    if limit < 1:
        return []
    now = _utc_now()

    def _read(conn: Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT r.id, r.channel, r.client_id, r.kind, r.routine_key, r.label,
                   r.schedule, r.enabled, r.next_fire_at, r.last_fired_at, r.metadata,
                   s.last_inbound_at, s.timezone, s.reminders_paused,
                   s.morning_pending_on, s.morning_pending_payload
            FROM reminders.reminder r
            JOIN reminders.user_channel_state s
              ON s.channel = r.channel AND s.client_id = r.client_id
            WHERE r.enabled = true
              AND r.next_fire_at IS NOT NULL
              AND r.next_fire_at <= %s
            ORDER BY r.next_fire_at
            LIMIT %s
            FOR UPDATE OF r SKIP LOCKED
            """,
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ``FOR UPDATE`` uses the write/transaction path (with retry).
    return _run_write(_read)


def apply_fire_outcome(
    reminder_id: UUID,
    *,
    channel: str,
    client_id: str,
    kind: str,
    outcome: str,
    detail: dict[str, Any],
    fired_at: datetime,
    next_fire_at: datetime | None,
    morning_pending_on: date | None = None,
    morning_pending_payload: dict[str, Any] | None = None,
    clear_morning_pending: bool = False,
) -> None:
    """
    Record a fire attempt, advance schedule, and optionally update morning pending state.

    Args:
        reminder_id: Reminder row uuid.
        channel: Channel name.
        client_id: User id.
        kind: Reminder kind string.
        outcome: ``fire_log.outcome`` value.
        detail: JSON detail for ``fire_log``.
        fired_at: When the tick processed this row (UTC).
        next_fire_at: Next scheduled fire, or None when disabled.
        morning_pending_on: Calendar date for undelivered morning brief.
        morning_pending_payload: Optional payload for pending morning delivery.
        clear_morning_pending: When True, clears pending morning fields on user state.

    Raises:
        psycopg.Error: When the database transaction fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()

    def _write(conn: Connection) -> None:
        conn.execute(
            """
            UPDATE reminders.reminder
            SET last_fired_at = %s,
                next_fire_at = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (fired_at, next_fire_at, reminder_id),
        )
        conn.execute(
            """
            INSERT INTO reminders.fire_log (
                reminder_id, channel, client_id, kind, outcome, detail, fired_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                reminder_id,
                ch,
                cid,
                kind,
                outcome,
                Jsonb(detail),
                fired_at,
            ),
        )
        if clear_morning_pending:
            conn.execute(
                """
                UPDATE reminders.user_channel_state
                SET morning_pending_on = NULL,
                    morning_pending_payload = NULL,
                    updated_at = now()
                WHERE channel = %s AND client_id = %s
                """,
                (ch, cid),
            )
        elif morning_pending_on is not None:
            conn.execute(
                """
                UPDATE reminders.user_channel_state
                SET morning_pending_on = %s,
                    morning_pending_payload = %s,
                    updated_at = now()
                WHERE channel = %s AND client_id = %s
                """,
                (
                    morning_pending_on,
                    Jsonb(morning_pending_payload or {}),
                    ch,
                    cid,
                ),
            )

    _run_write(_write)


def clear_morning_pending_state(channel: str, client_id: str) -> None:
    """
    Clear pending morning brief flags after successful delivery.

    Args:
        channel: Channel name.
        client_id: User id.

    Raises:
        ValueError: When ``channel`` is invalid.
        psycopg.Error: When the database write fails after retry.
    """
    ch = normalize_channel(channel)
    cid = client_id.strip()

    def _write(conn: Connection) -> None:
        conn.execute(
            """
            UPDATE reminders.user_channel_state
            SET morning_pending_on = NULL,
                morning_pending_payload = NULL,
                updated_at = now()
            WHERE channel = %s AND client_id = %s
            """,
            (ch, cid),
        )

    _run_write(_write)

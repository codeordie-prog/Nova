-- 005_reminders.sql
-- User reminders: morning brief + routine slots (WhatsApp-first; channel column for Telegram).
--
-- Requires Postgres 13+ for gen_random_uuid().

CREATE SCHEMA IF NOT EXISTS reminders;

CREATE TABLE reminders.user_channel_state (
    channel                  text        NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
    client_id                text        NOT NULL,
    last_inbound_at          timestamptz,
    timezone                 text        NOT NULL DEFAULT 'Africa/Nairobi',
    reminders_paused         boolean     NOT NULL DEFAULT false,
    morning_pending_on       date,
    morning_pending_payload  jsonb,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, client_id)
);

COMMENT ON TABLE reminders.user_channel_state IS
    'Per-channel user context for dispatch: 24h window anchor, TZ, pause, pending morning brief.';

CREATE TABLE reminders.reminder (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    channel         text        NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
    client_id       text        NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('morning_briefing', 'routine_reminder')),
    routine_key     text,
    label           text,
    schedule        jsonb       NOT NULL,
    enabled         boolean     NOT NULL DEFAULT true,
    next_fire_at    timestamptz,
    last_fired_at   timestamptz,
    metadata        jsonb       NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT reminder_routine_key_when_routine CHECK (
        (kind = 'routine_reminder' AND routine_key IS NOT NULL AND routine_key <> '')
        OR (kind = 'morning_briefing' AND routine_key IS NULL)
    )
);

CREATE UNIQUE INDEX reminder_morning_unique_idx
    ON reminders.reminder (channel, client_id)
    WHERE kind = 'morning_briefing';

CREATE UNIQUE INDEX reminder_routine_key_unique_idx
    ON reminders.reminder (channel, client_id, routine_key)
    WHERE kind = 'routine_reminder';

CREATE INDEX reminder_due_idx
    ON reminders.reminder (next_fire_at)
    WHERE enabled = true AND next_fire_at IS NOT NULL;

COMMENT ON COLUMN reminders.reminder.schedule IS
    'JSON schedule spec, e.g. {"type":"daily_time","local_time":"07:00"} or {"type":"interval_hours","every_hours":4}.';

CREATE TABLE reminders.fire_log (
    id           bigserial PRIMARY KEY,
    reminder_id  uuid        NOT NULL REFERENCES reminders.reminder (id),
    channel      text        NOT NULL,
    client_id    text        NOT NULL,
    kind         text        NOT NULL,
    outcome      text        NOT NULL CHECK (
        outcome IN (
            'sent',
            'skipped_outside_window',
            'skipped_paused',
            'skipped_disabled',
            'failed'
        )
    ),
    detail       jsonb       NOT NULL DEFAULT '{}',
    fired_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX fire_log_reminder_fired_idx ON reminders.fire_log (reminder_id, fired_at DESC);

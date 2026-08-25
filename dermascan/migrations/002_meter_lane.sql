-- 002_meter_lane.sql
-- Token metering and hold/settle reservations.
--
-- Brings the meter lane under the migration runner. It was first created by
-- hand from cloud_sql/billing/schema.sql, so every statement here is written to
-- be a no-op on a database that already has those objects.
--
-- Credits are KES: one credit is one shilling. Token costs accumulate in
-- micro-credits (1e-6 credit) and round to whole credits once, at settle.

-- ---------------------------------------------------------------------------
-- ledger: the meter lane writes kinds and ref_types the Paystack lane does not
-- ---------------------------------------------------------------------------
ALTER TABLE billing.ledger DROP CONSTRAINT IF EXISTS ledger_kind_check;
ALTER TABLE billing.ledger ADD CONSTRAINT ledger_kind_check
    CHECK (kind IN ('topup', 'debit', 'grant', 'refund', 'adjustment', 'hold', 'release'));

ALTER TABLE billing.ledger DROP CONSTRAINT IF EXISTS ledger_ref_type_check;
ALTER TABLE billing.ledger ADD CONSTRAINT ledger_ref_type_check
    CHECK (ref_type IN ('payment', 'trip', 'manual', 'grant', 'reservation', 'usage_settlement'));

-- ---------------------------------------------------------------------------
-- reservation: a credit ceiling held for one billable scope
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing.reservation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid        NOT NULL REFERENCES billing.account (id),
    reservation_key text        NOT NULL UNIQUE,
    job_type        text        NOT NULL,
    hold_credits    bigint      NOT NULL,
    actual_credits  bigint,
    state           text        NOT NULL CHECK (state IN ('held', 'committed', 'released')),
    case_id         text,
    client_id       text,
    channel         text,
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    settled_at      timestamptz
);

COMMENT ON COLUMN billing.reservation.reservation_key IS
    'request_id for an orchestrator turn, medgemma_job_id for an inference job. Unique, so a redelivered job reuses the hold instead of placing a second one.';
COMMENT ON COLUMN billing.reservation.hold_credits IS
    'Ceiling, not a charge. The hold debits the balance immediately and the unused remainder is refunded at settle, so an oversized hold is visible to the user as a mid-turn drop.';

-- Drives the stale-hold sweeper.
CREATE INDEX IF NOT EXISTS reservation_held_expires_idx
    ON billing.reservation (expires_at)
    WHERE state = 'held';

-- ---------------------------------------------------------------------------
-- usage_line: what a turn actually cost, balance-neutral until settle
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing.usage_line (
    id                 bigserial PRIMARY KEY,
    account_id         uuid        NOT NULL REFERENCES billing.account (id),
    reservation_id     uuid        REFERENCES billing.reservation (id),
    scope_key          text        NOT NULL,
    line_seq           int         NOT NULL,
    meter_type         text        NOT NULL CHECK (meter_type IN ('llm', 'tool', 'inference')),
    provider           text,
    model              text,
    purpose            text,
    input_tokens       int         NOT NULL DEFAULT 0,
    output_tokens      int         NOT NULL DEFAULT 0,
    cost_micro_credits bigint      NOT NULL DEFAULT 0,
    cost_credits       bigint      NOT NULL DEFAULT 0,
    idempotency_key    text        NOT NULL UNIQUE,
    metadata           jsonb       NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_key, line_seq)
);

-- Present on databases created from the hand-run schema, which had no micro column.
ALTER TABLE billing.usage_line
    ADD COLUMN IF NOT EXISTS cost_micro_credits bigint NOT NULL DEFAULT 0;

COMMENT ON COLUMN billing.usage_line.cost_micro_credits IS
    'Authoritative cost in 1e-6 credits. Rounding per line would floor every LLM call at one whole shilling and overcharge a multi-call turn several times over; settle rounds the scope total once.';
COMMENT ON COLUMN billing.usage_line.cost_credits IS
    'Display only. Derived from cost_micro_credits; never summed for settlement.';

CREATE INDEX IF NOT EXISTS usage_line_scope_idx
    ON billing.usage_line (scope_key, created_at DESC);

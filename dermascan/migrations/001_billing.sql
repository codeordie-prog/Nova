-- 001_billing.sql
-- Credit accounts, append-only ledger, and Paystack payment records.
--
-- No reservation table: credits are deducted destructively at each checkpoint
-- in Redis and settled to this schema once per trip, so there is no "held"
-- state to model here.
--
-- Requires Postgres 13+ for gen_random_uuid().

CREATE TABLE billing.account (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel              text        NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
    client_id            text        NOT NULL,
    owner_id             uuid,
    email                text,
    msisdn               text,
    currency             char(3)     NOT NULL DEFAULT 'KES',
    balance_credits      bigint      NOT NULL DEFAULT 0,
    free_credits_granted boolean     NOT NULL DEFAULT false,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, client_id)
);

COMMENT ON COLUMN billing.account.balance_credits IS
    'Spendable credits. May go negative up to the overdraft floor so an in-flight trip can finish.';
COMMENT ON COLUMN billing.account.msisdn IS
    'M-PESA number, only ever written from a value the user volunteered. Never derived from a channel payload.';

-- Append-only. Never UPDATE, never DELETE. This is the audit trail.
CREATE TABLE billing.ledger (
    id              bigserial PRIMARY KEY,
    account_id      uuid        NOT NULL REFERENCES billing.account (id),
    delta_credits   bigint      NOT NULL,
    balance_after   bigint      NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('topup', 'debit', 'grant', 'refund', 'adjustment')),
    job_type        text,
    ref_type        text        NOT NULL CHECK (ref_type IN ('payment', 'trip', 'manual', 'grant')),
    ref_id          text        NOT NULL,
    idempotency_key text        NOT NULL UNIQUE,
    metadata        jsonb       NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ledger_account_created_idx ON billing.ledger (account_id, created_at DESC);

COMMENT ON COLUMN billing.ledger.idempotency_key IS
    'Dedupe spine. Paystack replays webhooks and streams redeliver; a repeated key must not credit twice.';

CREATE TABLE billing.payment_intent (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid        NOT NULL REFERENCES billing.account (id),
    reference         text        NOT NULL UNIQUE,
    amount_minor      bigint      NOT NULL CHECK (amount_minor > 0),
    currency          char(3)     NOT NULL,
    credits_to_grant  bigint      NOT NULL CHECK (credits_to_grant > 0),
    status            text        NOT NULL CHECK (status IN ('initialized', 'pending', 'success', 'failed', 'abandoned')),
    method            text        NOT NULL CHECK (method IN ('checkout', 'stk')),
    channel_used      text,
    authorization_url text,
    msisdn            text,
    raw               jsonb       NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Drives the verify poller: unresolved intents, oldest first.
CREATE INDEX payment_intent_unresolved_idx ON billing.payment_intent (created_at)
    WHERE status IN ('initialized', 'pending');

COMMENT ON COLUMN billing.payment_intent.reference IS
    'qutis_{acct8}_{ulid}. The tracking code that ties a payment back to one account regardless of how it was paid.';
COMMENT ON COLUMN billing.payment_intent.amount_minor IS
    'Subunits. KES 100 is 10000. Paystack rejects major units silently by charging 100x wrong.';

CREATE TABLE billing.paystack_event (
    id           bigserial PRIMARY KEY,
    dedupe_key   text        NOT NULL UNIQUE,
    event        text        NOT NULL,
    reference    text,
    payload      jsonb       NOT NULL,
    status       text        NOT NULL CHECK (status IN ('received', 'processed', 'ignored', 'error')),
    received_at  timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

CREATE INDEX paystack_event_reference_idx ON billing.paystack_event (reference);

-- Versioned config: credit prices, per-model token rates, and scalar settings.
-- Rows are added, never updated, so a past charge can always be explained by
-- the rate that was in force when it ran.
CREATE TABLE billing.price_list (
    id             bigserial PRIMARY KEY,
    scope          text        NOT NULL CHECK (scope IN ('job_credits', 'model_token_rate', 'setting')),
    key            text        NOT NULL,
    value_numeric  numeric(18, 8) NOT NULL,
    effective_from timestamptz NOT NULL DEFAULT now(),
    created_by     text,
    metadata       jsonb       NOT NULL DEFAULT '{}',
    UNIQUE (scope, key, effective_from)
);

CREATE INDEX price_list_lookup_idx ON billing.price_list (scope, key, effective_from DESC);

-- Seed v1 from billing-credits-plan.md section 3. 1 credit = KES 1.00.
INSERT INTO billing.price_list (scope, key, value_numeric, created_by) VALUES
    ('job_credits', 'chat_turn',             0,   'migration:001'),
    ('job_credits', 'skin_audit',            20,  'migration:001'),
    ('job_credits', 'product_review_image',  25,  'migration:001'),
    ('job_credits', 'product_review_text',   15,  'migration:001'),
    ('job_credits', 'deep_research',         5,   'migration:001'),
    ('job_credits', 'specialist_escalation', 40,  'migration:001'),
    ('setting',     'free_trial_credits',    40,  'migration:001'),
    ('setting',     'min_topup_kes',         100, 'migration:001'),
    ('setting',     'usd_kes_rate',          129, 'migration:001'),
    ('setting',     'overdraft_floor',       -60, 'migration:001');

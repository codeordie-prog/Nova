-- Billing schema (unified: meter/hold lane + Paystack lane)
-- Decisions: cloud_sql/billing/PHASE0_DECISIONS.md
-- Alignment:  cloud_sql/billing/SCHEMA_ALIGNMENT.md
--
-- Fresh install: run this file only.
-- Existing DB (schema.sql already applied): run 002_align_paystack_schema.sql

CREATE SCHEMA IF NOT EXISTS billing;

CREATE TABLE billing.account (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel              text NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
  client_id            text NOT NULL,              -- +2547… | telegram user id
  owner_id             uuid,                       -- links channels to one human, later
  email                text,                       -- Paystack; collected at first top-up
  msisdn               text,                       -- M-PESA; user-volunteered only
  currency             char(3) NOT NULL DEFAULT 'KES',
  balance_credits      bigint NOT NULL DEFAULT 0,
  free_credits_granted boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, client_id)
);

COMMENT ON COLUMN billing.account.balance_credits IS
  'Spendable credits. Hold lane blocks at insufficient balance; Paystack lane may allow overdraft per price_list setting.';
COMMENT ON COLUMN billing.account.msisdn IS
  'M-PESA number volunteered by the user. Never derived from channel payload.';

-- Append-only. Never UPDATE or DELETE.
-- kind superset: grant|topup|hold|release|debit|refund|adjustment
-- ref_type superset: payment|trip|manual|grant|reservation|usage_settlement
-- Intentionally NO CHECK constraints — two lanes write different enum values.
CREATE TABLE billing.ledger (
  id              bigserial PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES billing.account(id),
  delta_credits   bigint NOT NULL,
  balance_after   bigint NOT NULL,
  kind            text NOT NULL,
  job_type        text,
  ref_type        text NOT NULL,
  ref_id          text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  metadata        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ledger_account_created_idx ON billing.ledger (account_id, created_at DESC);

COMMENT ON COLUMN billing.ledger.idempotency_key IS
  'Dedupe spine for Paystack replays, stream redelivery, and settle retries.';

-- Meter/hold lane -------------------------------------------------------------

CREATE TABLE billing.reservation (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      uuid NOT NULL REFERENCES billing.account(id),
  reservation_key text NOT NULL UNIQUE,            -- request_id | medgemma_job_id | group_id
  job_type        text NOT NULL,
  hold_credits    bigint NOT NULL,
  actual_credits  bigint,
  state           text NOT NULL CHECK (state IN ('held', 'committed', 'released')),
  case_id         text,
  client_id       text,
  channel         text,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  settled_at      timestamptz
);

CREATE INDEX reservation_held_expires_idx
  ON billing.reservation (state, expires_at)
  WHERE state = 'held';

CREATE TABLE billing.usage_line (
  id              bigserial PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES billing.account(id),
  reservation_id  uuid REFERENCES billing.reservation(id),
  scope_key       text NOT NULL,
  line_seq        int NOT NULL,
  meter_type      text NOT NULL CHECK (meter_type IN ('llm', 'tool')),
  provider        text,
  model           text,
  purpose         text,                            -- agent_step | summarization | tavily
  input_tokens    int NOT NULL DEFAULT 0,
  output_tokens   int NOT NULL DEFAULT 0,
  cost_credits    bigint NOT NULL DEFAULT 0,
  idempotency_key text NOT NULL UNIQUE,
  metadata        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope_key, line_seq)
);

CREATE INDEX usage_line_scope_idx ON billing.usage_line (scope_key, created_at DESC);

-- Paystack lane ---------------------------------------------------------------

CREATE TABLE billing.payment_intent (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id         uuid NOT NULL REFERENCES billing.account(id),
  reference          text NOT NULL UNIQUE,
  amount_minor       bigint NOT NULL CHECK (amount_minor > 0),
  currency           char(3) NOT NULL,
  credits_to_grant   bigint NOT NULL CHECK (credits_to_grant > 0),
  status             text NOT NULL CHECK (status IN ('initialized', 'pending', 'success', 'failed', 'abandoned')),
  method             text NOT NULL CHECK (method IN ('checkout', 'stk')) DEFAULT 'checkout',
  channel_used       text,
  authorization_url  text,
  msisdn             text,
  raw                jsonb NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX payment_intent_unresolved_idx ON billing.payment_intent (created_at)
  WHERE status IN ('initialized', 'pending');

COMMENT ON COLUMN billing.payment_intent.reference IS
  'qutis_{acct8}_{ulid}. Ties payment to account regardless of method.';
COMMENT ON COLUMN billing.payment_intent.amount_minor IS
  'Subunits. KES 100 = 10000 minor units.';

CREATE TABLE billing.paystack_event (
  id           bigserial PRIMARY KEY,
  dedupe_key   text NOT NULL UNIQUE,
  event        text NOT NULL,
  reference    text,
  payload      jsonb NOT NULL,
  status       text NOT NULL CHECK (status IN ('received', 'processed', 'ignored', 'error')),
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);

CREATE INDEX paystack_event_reference_idx ON billing.paystack_event (reference);

CREATE TABLE billing.price_list (
  id             bigserial PRIMARY KEY,
  scope          text NOT NULL CHECK (scope IN ('job_credits', 'model_token_rate', 'setting')),
  key            text NOT NULL,
  value_numeric  numeric(18, 8) NOT NULL,
  effective_from timestamptz NOT NULL DEFAULT now(),
  created_by     text,
  metadata       jsonb NOT NULL DEFAULT '{}',
  UNIQUE (scope, key, effective_from)
);

CREATE INDEX price_list_lookup_idx ON billing.price_list (scope, key, effective_from DESC);

COMMENT ON TABLE billing.price_list IS
  'Versioned prices for Paystack lane (job SKUs, settings). Token rates for meter lane: billing_config.py.';

-- Paystack-lane seed defaults (1 credit = KES 1.00 for job SKUs)
INSERT INTO billing.price_list (scope, key, value_numeric, created_by) VALUES
  ('job_credits', 'chat_turn',             0,   'migration:schema'),
  ('job_credits', 'skin_audit',            20,  'migration:schema'),
  ('job_credits', 'product_review_image',  25,  'migration:schema'),
  ('job_credits', 'product_review_text',   15,  'migration:schema'),
  ('job_credits', 'deep_research',         5,   'migration:schema'),
  ('job_credits', 'specialist_escalation', 40,  'migration:schema'),
  ('setting',     'free_trial_credits',    40,  'migration:schema'),
  ('setting',     'min_topup_kes',         100, 'migration:schema'),
  ('setting',     'usd_kes_rate',          129, 'migration:schema'),
  ('setting',     'overdraft_floor',       -60, 'migration:schema');

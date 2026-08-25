-- 002_align_paystack_schema.sql
-- Reconcile live billing schema (hold/meter/reservation lane) with Paystack lane
-- (feat/billing-paystack-credits 001_billing.sql).
--
-- Safe to run on qutis_db after cloud_sql/billing/schema.sql.
-- Idempotent where practical (IF NOT EXISTS / DO blocks).

BEGIN;

-- ---------------------------------------------------------------------------
-- account: Paystack lane needs msisdn + default uuid
-- ---------------------------------------------------------------------------
ALTER TABLE billing.account
    ADD COLUMN IF NOT EXISTS msisdn text;

COMMENT ON COLUMN billing.account.msisdn IS
    'M-PESA number volunteered by the user at top-up. Never derived from channel payload.';

ALTER TABLE billing.account
    ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- Optional channel guard (skip if you add channels later via migration).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'account_channel_check'
          AND conrelid = 'billing.account'::regclass
    ) THEN
        ALTER TABLE billing.account
            ADD CONSTRAINT account_channel_check
            CHECK (channel IN ('whatsapp', 'telegram'));
    END IF;
END $$;

COMMENT ON COLUMN billing.account.balance_credits IS
    'Spendable credits. Hold/meter lane may block at zero; Paystack lane may allow negative up to overdraft_floor setting.';

-- ---------------------------------------------------------------------------
-- ledger: superset kinds/ref_types — no CHECK added (both lanes write here)
-- hold/release/reservation (meter lane) + topup/debit/trip (Paystack lane)
-- ---------------------------------------------------------------------------
COMMENT ON TABLE billing.ledger IS
    'Append-only. kind superset: grant|topup|hold|release|debit|refund|adjustment. '
    'ref_type superset: payment|trip|manual|grant|reservation|usage_settlement.';

COMMENT ON COLUMN billing.ledger.idempotency_key IS
    'Dedupe spine. Paystack webhooks, stream redelivery, and settle retries must not double-apply.';

-- ---------------------------------------------------------------------------
-- payment_intent: Paystack lane columns
-- ---------------------------------------------------------------------------
ALTER TABLE billing.payment_intent
    ADD COLUMN IF NOT EXISTS method text;

ALTER TABLE billing.payment_intent
    ADD COLUMN IF NOT EXISTS msisdn text;

-- Backfill method for any pre-existing rows, then enforce NOT NULL + CHECK.
UPDATE billing.payment_intent
SET method = 'checkout'
WHERE method IS NULL;

ALTER TABLE billing.payment_intent
    ALTER COLUMN method SET DEFAULT 'checkout';

ALTER TABLE billing.payment_intent
    ALTER COLUMN method SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_intent_method_check'
          AND conrelid = 'billing.payment_intent'::regclass
    ) THEN
        ALTER TABLE billing.payment_intent
            ADD CONSTRAINT payment_intent_method_check
            CHECK (method IN ('checkout', 'stk'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_intent_status_check'
          AND conrelid = 'billing.payment_intent'::regclass
    ) THEN
        ALTER TABLE billing.payment_intent
            ADD CONSTRAINT payment_intent_status_check
            CHECK (status IN ('initialized', 'pending', 'success', 'failed', 'abandoned'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_intent_amount_minor_check'
          AND conrelid = 'billing.payment_intent'::regclass
    ) THEN
        ALTER TABLE billing.payment_intent
            ADD CONSTRAINT payment_intent_amount_minor_check
            CHECK (amount_minor > 0);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'payment_intent_credits_to_grant_check'
          AND conrelid = 'billing.payment_intent'::regclass
    ) THEN
        ALTER TABLE billing.payment_intent
            ADD CONSTRAINT payment_intent_credits_to_grant_check
            CHECK (credits_to_grant > 0);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS payment_intent_unresolved_idx
    ON billing.payment_intent (created_at)
    WHERE status IN ('initialized', 'pending');

COMMENT ON COLUMN billing.payment_intent.reference IS
    'qutis_{acct8}_{ulid}. Ties a payment to one account regardless of payment method.';
COMMENT ON COLUMN billing.payment_intent.amount_minor IS
    'Subunits. KES 100 = 10000 minor units.';

ALTER TABLE billing.payment_intent
    ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- ---------------------------------------------------------------------------
-- paystack_event: indexes + status CHECK
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS paystack_event_reference_idx
    ON billing.paystack_event (reference);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'paystack_event_status_check'
          AND conrelid = 'billing.paystack_event'::regclass
    ) THEN
        ALTER TABLE billing.paystack_event
            ADD CONSTRAINT paystack_event_status_check
            CHECK (status IN ('received', 'processed', 'ignored', 'error'));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- price_list: Paystack lane + admin portal (job SKUs, settings)
-- Token rates for meter lane stay in billing_config.py / env for now.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing.price_list (
    id             bigserial PRIMARY KEY,
    scope          text NOT NULL,
    key            text NOT NULL,
    value_numeric  numeric(18, 8) NOT NULL,
    effective_from timestamptz NOT NULL DEFAULT now(),
    created_by     text,
    metadata       jsonb NOT NULL DEFAULT '{}',
    UNIQUE (scope, key, effective_from)
);

CREATE INDEX IF NOT EXISTS price_list_lookup_idx
    ON billing.price_list (scope, key, effective_from DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'price_list_scope_check'
          AND conrelid = 'billing.price_list'::regclass
    ) THEN
        ALTER TABLE billing.price_list
            ADD CONSTRAINT price_list_scope_check
            CHECK (scope IN ('job_credits', 'model_token_rate', 'setting'));
    END IF;
END $$;

COMMENT ON TABLE billing.price_list IS
    'Versioned prices. Rows are inserted, never updated. Paystack lane reads job_credits + settings.';

-- Seed Paystack-lane defaults only when empty (colleague 001_billing.sql).
INSERT INTO billing.price_list (scope, key, value_numeric, created_by)
SELECT v.scope, v.key, v.value_numeric, v.created_by
FROM (VALUES
    ('job_credits', 'chat_turn',             0::numeric,   'migration:002'),
    ('job_credits', 'skin_audit',            20::numeric,  'migration:002'),
    ('job_credits', 'product_review_image',  25::numeric,  'migration:002'),
    ('job_credits', 'product_review_text',   15::numeric,  'migration:002'),
    ('job_credits', 'deep_research',         5::numeric,   'migration:002'),
    ('job_credits', 'specialist_escalation', 40::numeric,  'migration:002'),
    ('setting',     'free_trial_credits',    40::numeric,  'migration:002'),
    ('setting',     'min_topup_kes',         100::numeric, 'migration:002'),
    ('setting',     'usd_kes_rate',          129::numeric, 'migration:002'),
    ('setting',     'overdraft_floor',       -60::numeric, 'migration:002')
) AS v(scope, key, value_numeric, created_by)
WHERE NOT EXISTS (SELECT 1 FROM billing.price_list LIMIT 1);

COMMIT;

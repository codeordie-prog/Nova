-- 006_billing_agent_channels.sql
-- Allow agent API ingress channels on billing.account (OpenClaw / poll clients).

ALTER TABLE billing.account
    DROP CONSTRAINT IF EXISTS account_channel_check;

ALTER TABLE billing.account
    ADD CONSTRAINT account_channel_check
        CHECK (channel IN ('whatsapp', 'telegram', 'web', 'internal'));
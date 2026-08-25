-- 003_meter_settings.sql
-- Move the meter lane's tunables out of the environment and into price_list,
-- so they are editable from the admin portal and a past charge stays
-- explainable by the row that was in force when it ran.
--
-- Values are KES credits.
--
-- min_balance_credits is 0 deliberately. A hold already debits the balance and
-- guarantees the funds; requiring an additional floor on top is a second gate,
-- and at the previous 150 it made the product unusable: 150 hold + 150 floor
-- put a 40-credit trial user and a KES 100 top-up both below the threshold to
-- run a single turn.
--
-- The hold ceilings below are interim and ship with enforcement off. Size them
-- from the p99 of billing.usage_line after a metering run, not from a guess.

INSERT INTO billing.price_list (scope, key, value_numeric, created_by) VALUES
    ('setting', 'min_balance_credits',       0,  'migration:003'),
    ('setting', 'orchestrator_hold_credits', 25, 'migration:003'),
    ('setting', 'medgemma_hold_credits',     60, 'migration:003'),
    ('setting', 'reservation_ttl_sec',       900, 'migration:003');

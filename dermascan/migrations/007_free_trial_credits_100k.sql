-- 007_free_trial_credits_100k.sql
-- One-time free grant for new billing accounts (first contact via any channel).
-- Supersedes migration:001 value (40) via newer effective_from.

INSERT INTO billing.price_list (scope, key, value_numeric, created_by) VALUES
    ('setting', 'free_trial_credits', 100000, 'migration:007');
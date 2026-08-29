-- 004_price_list_cleanup.sql
-- Make price_list describe only prices that are actually charged.
--
-- Every row here was either priced-but-unread or charged-but-unpriced, and both
-- mislead: the admin portal renders price_list as the rate card, so a row that
-- nothing reads reads as a live price to whoever is setting it.
--
-- What charges what, after this migration:
--   * orchestrator turns, text product review, research  -> token-metered, no
--     flat price. A flat SKU on top would double-charge.
--   * skin_audit, product_review_image, combined_audit   -> MedGemma GPU
--     inference. Not in BILLING_TOKEN_RATES_USD, so the token meter sees
--     nothing and a flat SKU is the only unit available.
--   * specialist_escalation                              -> human review. No
--     tokens by definition.

-- combined_audit is a real BillingJobType that reaches the MedGemma charge
-- path, but had no row. credits_for_medgemma_job swallowed the lookup failure
-- and returned 0, so combined audits were free. Priced as the two jobs it
-- performs (skin_audit 20 + product_review_image 25) less a bundle discount.
INSERT INTO billing.price_list (scope, key, value_numeric, created_by) VALUES
    ('job_credits', 'combined_audit', 40, 'migration:004');

-- Retire rows nothing reads. A zero row still renders in the portal, so these
-- are deleted rather than zeroed.
--   chat_turn          - orchestrator turns are token-metered; the row was 0.
--   product_review_text- not a BillingJobType; the text path is token-metered.
--   deep_research      - not a BillingJobType; no charging code exists.
--   overdraft_floor    - only redis_wallet.deduct() takes an overdraft floor,
--                        and it takes it as a parameter defaulting to 0. The
--                        setting was never read.
DELETE FROM billing.price_list
WHERE (scope = 'job_credits' AND key IN ('chat_turn', 'product_review_text', 'deep_research'))
   OR (scope = 'setting'     AND key = 'overdraft_floor');

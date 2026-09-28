# Billing schema

One schema, one migration mechanism. This file used to describe two lanes
reconciling across branches; they are merged, and the split no longer exists.

## Applying it

`dermascan/migrations/*.sql`, applied in filename order by
`dermascan/scripts/migrate.py`, tracked in `billing.schema_migrations`
(version, checksum, applied_at). Forward-only. Editing an applied migration is
an error — write the next numbered file instead.

```bash
poetry run python -m dermascan.scripts.migrate --dry-run   # list pending
poetry run python -m dermascan.scripts.migrate             # apply
```

If `qutis_db` was created from the old hand-run scripts and already has
`billing.account` et al., stamp the baseline then apply the rest:

```bash
poetry run python -m dermascan.scripts.migrate --stamp 001_billing
poetry run python -m dermascan.scripts.migrate
```

`cloud_sql/billing/schema.sql` and `002_align_paystack_schema.sql` were the
hand-run originals. They are deleted. `002_meter_lane.sql` reproduces what they
created, written `IF NOT EXISTS` throughout, so it is a no-op on `qutis_db`
where the hand-run version was already applied.

## Tables

| Table | Purpose |
|---|---|
| `account` | One wallet per `(channel, client_id)`. `balance_credits` is a cached sum. |
| `ledger` | Append-only. The only writer of `balance_credits`. `idempotency_key` is the dedupe spine. |
| `reservation` | A credit ceiling held for one scope — `request_id` for a turn, `medgemma_job_id` for an inference job. |
| `usage_line` | What a scope actually cost, in micro-credits. Balance-neutral until settle. |
| `payment_intent` | Paystack checkout/STK lifecycle. `reference` ties money back to one account. |
| `paystack_event` | Webhook dedupe and audit. |
| `price_list` | Versioned prices, token rates and settings. Rows are inserted, never updated. |
| `schema_migrations` | Migration bookkeeping. |

**Invariant:** `account.balance_credits = SUM(ledger.delta_credits)`. Reconciliation asserts it; drift is a P1.

## The credit unit

**One credit is one Kenyan shilling.** It is what a user tops up through
Paystack, what they see in chat, and what they spend.

Provider token prices are quoted in USD per million tokens. That is a *cost
input*, not the wallet unit: `billing/config.py` converts USD list prices
through the `usd_kes_rate` setting in `price_list` when the rate card loads.
There is no separate USD credit scale.

Token costs accumulate on `usage_line.cost_micro_credits` (1e-6 credit) and are
rounded to whole credits once, when a reservation settles. Rounding per line
would floor every LLM call at a whole shilling and overcharge a multi-call turn
several times over.

## What is data, not environment

Prices, token rates, hold ceilings, the free-trial grant, the top-up minimum
and `usd_kes_rate` all live in `price_list` and are edited from `/admin/pricing`.
Changing one does not need a deploy, and a past charge stays explainable by the
row that was in force when it ran.

Environment holds only what is operational: `BILLING_ENABLED`,
`BILLING_METER_ENABLED`, `BILLING_ENFORCE`, `PAYSTACK_SECRET_KEY`,
`PAYSTACK_CALLBACK_URL`.

## Hold ceilings are not yet sized

`orchestrator_hold_credits = 25` and `medgemma_hold_credits = 60` are interim
values, shipped with enforcement off. Size them from the p99 of `usage_line`
after a metering run:

```sql
SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY total)
FROM (SELECT scope_key, SUM(cost_micro_credits) / 1000000.0 AS total
      FROM billing.usage_line GROUP BY scope_key) s;
```

`min_balance_credits` is `0` on purpose. A hold already debits the balance and
guarantees the funds; a floor on top of it is a second gate.

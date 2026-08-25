# Billing schema alignment

Two lanes share one Postgres `billing` schema. **No code merge required** — only
this schema contract and `002_align_paystack_schema.sql` on `qutis_db`.

| Lane | Owner | Flow | Code |
|------|-------|------|------|
| **Meter / hold** | You | hold → meter (`usage_line`) → settle (`reservation`) | `dermascan/billing/` — `accounts`, `ledger`, `reservations`, `usage`, `meter`, `config` |
| **Paystack / top-up** | Colleague | top-up → ledger `topup` | `dermascan/billing/` — `topups`, `paystack_client`, `redis_wallet`, `pricing`, … |

---

## Shared tables (both lanes)

### `billing.account`

| Column | Meter lane | Paystack lane |
|--------|------------|---------------|
| `(channel, client_id)` UNIQUE | lookup / auto-create | `accounts.get_or_create()` |
| `balance_credits` | updated on hold/settle | updated on topup/debit |
| `email` | optional until needed | required at first top-up |
| `msisdn` | unused | M-PESA number (user-provided) |
| `free_credits_granted` | one-time grant | free trial grant |

### `billing.ledger` (append-only)

**Do not add CHECK constraints** — the two lanes use different `kind` / `ref_type` values.

| kind | Written by |
|------|------------|
| `grant`, `topup`, `refund`, `adjustment` | Paystack lane |
| `hold`, `release`, `debit` | Meter lane (settle / overrun) |
| `debit` | Both (trip settle vs usage settle — use distinct `idempotency_key`) |

| ref_type | Written by |
|----------|------------|
| `payment` | Paystack top-up |
| `trip` | Colleague Redis settle (`trip:{request_id}`) — if they keep Redis path |
| `grant`, `manual` | Both |
| `reservation` | Meter lane hold/release |
| `usage_settlement` | Meter lane final debit metadata |

**Invariant (both):** `account.balance_credits = SUM(ledger.delta_credits)` — nightly verify.

---

## Lane-specific tables

### Meter lane only

| Table | Purpose |
|-------|---------|
| `billing.reservation` | Hold ceiling per `request_id` / job |
| `billing.usage_line` | Per-LLM token lines (balance-neutral until settle) |

Colleague code **does not read or write** these. Safe to use exclusively for your flow.

### Paystack lane only

| Table | Purpose |
|-------|---------|
| `billing.payment_intent` | Checkout / STK lifecycle |
| `billing.paystack_event` | Webhook dedupe |
| `billing.price_list` | Job SKU prices + settings (`free_trial_credits`, `min_topup_kes`, …) |

Your meter lane **does not need** `price_list` for token rates (those live in
`dermascan/billing/config.py` / env). Colleague **does** need `price_list` for deliverable SKUs.

---

## Apply on live DB

You already ran `schema.sql`. Run:

```powershell
psql -h 34.79.111.107 -p 5432 -U postgres -d qutis_db -f cloud_sql/billing/002_align_paystack_schema.sql
```

Verify:

```sql
\d billing.account          -- msisdn present
\d billing.payment_intent   -- method, msisdn present
\dt billing.*               -- price_list added
SELECT scope, key, value_numeric FROM billing.price_list ORDER BY scope, key;
```

---

## Open product decision (not schema)

**Credit definition:**

| Source | Model |
|--------|--------|
| Colleague `price_list` seeds | **1 credit = KES 1.00** (skin_audit = 20 credits) |
| Your `billing_config.py` | **100 credits = $1.00 USD** API cost |

Same `bigint` column — different semantics. Align before go-live (e.g. pick KES
retail credits and map token rates through `usd_kes_rate`, or adopt micro-credits).

Schema supports either; code must agree on one scale.

---

## What colleague code expects vs your DB (after 002)

| Expectation | After 002 |
|-------------|-----------|
| `billing.account.msisdn` | ✅ |
| `payment_intent.method` NOT NULL | ✅ (default `checkout`) |
| `payment_intent` status enum CHECK | ✅ |
| `price_list` + seeds | ✅ |
| `paystack_event` reference index | ✅ |
| No `reservation` / `usage_line` | ✅ present but ignored by their code |
| Ledger CHECK (kind/ref_type only trip/payment) | ✅ **not added** — would break hold/release |

Colleague can deploy Paystack against this DB without touching your tables.

---

## Your next steps (meter lane, no merge)

1. Run `002_align_paystack_schema.sql` on `qutis_db`
2. Implement account resolver + hold/settle against `reservation` / `usage_line` / `ledger`
3. Wire metering hooks → `usage_line` + `credits_for_tokens()`
4. Agree credit scale with colleague before blocking users on balance

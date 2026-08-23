# `dermascan/billing/` package layout

Both lanes live here so merges with `feat/billing-paystack-credits` stay in one tree.

## Meter/hold lane (main — your flow)

| Module | Purpose |
|--------|---------|
| `config.py` | Token rate card, enums, hold ceilings (env-backed) |
| `db.py` | Postgres pool |
| `flags.py` | `BILLING_METER_ENABLED`, `BILLING_ENFORCE` |
| `context.py` | Per-turn `BillingScope` ContextVar |
| `accounts.py` | `get_or_create_account`, `get_balance` |
| `ledger.py` | Append-only ledger writes (`hold`, `release`, …) |
| `reservations.py` | `place_hold`, `commit_reservation`, `release_reservation` |
| `usage.py` | `usage_line` metering |
| `meter.py` | LLM token extraction + hook for model chain |

Public API: `from dermascan import billing` or `from dermascan.billing import …`

## Paystack lane (colleague branch — merge later)

Expected additions from `feat/billing-paystack-credits`:

| Module | Purpose |
|--------|---------|
| `pricing.py` | DB `price_list` lookups (job SKUs) |
| `redis_wallet.py` | Hot-path deduct/settle (optional; you use SQL hold) |
| `topups.py`, `intents.py`, `fulfilment.py`, `webhook.py` | Paystack |
| `paystack_client.py`, `notify.py`, `admin_queries.py` | Paystack + admin |
| `billing_worker.py` | Worker (under `backend/services/agents/`) |

## Expected merge conflicts

- **`accounts.py`** — both lanes create accounts; merge grant/top-up paths
- **`ledger.py`** — colleague adds `topup`/`trip` kinds; keep your `hold`/`release`
- **`flags.py`** — add colleague's `BILLING_ENABLED` alongside your meter flags
- **`db.py`** — should be identical or trivial diff

## Shims (remove after merge)

- `dermascan/shared/billing.py` → re-exports `dermascan.billing`
- `dermascan/shared/billing_config.py` → re-exports `dermascan.billing.config`

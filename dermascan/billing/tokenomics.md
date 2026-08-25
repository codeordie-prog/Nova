# Tokenomics — meter/hold billing

How Qutis bills an orchestrator turn and a MedGemma inference job: **reserve → accumulate → commit/refund**.

Source of truth is Cloud SQL schema `billing` (`cloud_sql/billing/schema.sql`).  
Code: `dermascan/billing/`. Rate card: `cloud_sql/billing/RATE_CARD.md`.

Internal credits are **cost-base**: **100 credits = $1.00 USD** of provider API list price (`BILLING_CREDITS_PER_USD`). Retail KES per credit is set at Paystack top-up and is not computed here.

---

## 1. Database schema

Two lanes share the same tables:

| Lane | Who writes | What it does |
|------|------------|--------------|
| **Meter/hold** (this document) | Orchestrator worker, MedGemma gateway + worker, sweeper | Reserve a ceiling, record usage, settle unused hold |
| **Paystack** (colleague branch) | Top-up / webhook | Grant credits after payment |

Invariant: `account.balance_credits = SUM(ledger.delta_credits)` for that account. The ledger is append-only. Every balance change has a unique `idempotency_key`.

### 1.1 `billing.account` — wallet

One wallet per messaging identity. Lookup is `UNIQUE (channel, client_id)`.

| Column | Utility |
|--------|---------|
| `id` | Stable wallet uuid. All ledger / reservation / usage rows point here. |
| `channel` | `whatsapp` or `telegram`. |
| `client_id` | Phone or Telegram user id from ingress. **Not** `case_id`. |
| `owner_id` | Reserved for “one human, many channels”. Unused in v1. |
| `email` / `msisdn` | Paystack / M-PESA. Collected at top-up, never derived from chat. |
| `currency` | Display/Paystack currency. Default `KES`. |
| `balance_credits` | Spendable credits. Updated only via `ledger.append`. |
| `free_credits_granted` | One-time free grant flag. |

Created on first billable ingress (`get_or_create_account`). Optional `ledger.kind=grant` if `BILLING_FREE_GRANT_CREDITS > 0`.

### 1.2 `billing.ledger` — every balance movement

Never `UPDATE` or `DELETE`. One row per mutation.

| Column | Utility |
|--------|---------|
| `delta_credits` | Signed move. Hold is negative; release/grant/top-up are positive. |
| `balance_after` | Snapshot after this row. |
| `kind` | `grant` \| `topup` \| `hold` \| `release` \| `debit` \| `refund` \| `adjustment` |
| `job_type` | `orchestrator_turn`, `skin_audit`, `product_review_image`, `combined_audit`, … |
| `ref_type` / `ref_id` | What this row is about (`reservation`, `payment`, `usage_settlement`, …). |
| `idempotency_key` | Dedupe for stream redelivery and settle retries. Replays are no-ops. |
| `metadata` | Hold size, actual vs refund, rate-card version, reason. |

| `kind` | When | Sign |
|--------|------|------|
| `hold` | Reservation placed | − ceiling |
| `release` | Unused hold returned (settle or cancel/expire) | + unused (or full hold on cancel) |
| `debit` | Overrun: actual usage exceeded the hold | − overrun |
| `grant` / `topup` | Free credits / Paystack | + |
| `refund` / `adjustment` | Payment reversal / ops | +/− |

**Usage lines do not move the balance.** Only ledger rows do.

### 1.3 `billing.reservation` — the hold

One reservation per billable scope.

| Column | Utility |
|--------|---------|
| `reservation_key` | Idempotency scope. Orchestrator: `request_id`. MedGemma: `medgemma_job_id`. |
| `job_type` | What the hold is for. |
| `hold_credits` | Ceiling reserved from the wallet. |
| `actual_credits` | Set at settle = `SUM(usage_line.cost_credits)` for that scope. |
| `state` | `held` → `committed` (settled) or `released` (cancelled / TTL expired). |
| `expires_at` | Sweeper refunds stale `held` rows (`BILLING_RESERVATION_TTL_SEC`, default 900s). |
| `case_id` / `client_id` / `channel` | Support correlation only. |

Same `reservation_key` is reused on retry (no second hold).

### 1.4 `billing.usage_line` — accumulation (no money movement)

Written **during** the job. Read **at settle**.

| Column | Utility |
|--------|---------|
| `scope_key` | Same as `reservation.reservation_key` (`request_id` or `job_id`). |
| `line_seq` | Monotonic line within the scope (orchestrator LLM steps). MedGemma uses `1`. |
| `meter_type` | `llm` (token-priced), `tool` (reserved, unused), `inference` (MedGemma flat SKU). |
| `provider` / `model` | Rate-card identity, e.g. `google` / `gemini-3.5-flash`. |
| `purpose` | Orchestrator: `agent_step` or `summarization`. MedGemma: job type label. |
| `input_tokens` / `output_tokens` | Orchestrator only. MedGemma writes `0` / `0`. |
| `cost_credits` | Integer credits for this line (round-up per token bucket, or flat SKU). |
| `idempotency_key` | `usage:{scope_key}:{line_seq}`. |

`commit_reservation` does `actual = SUM(cost_credits) WHERE scope_key = reservation_key`.

> Schema note: `schema.sql` currently CHECKs `meter_type IN ('llm', 'tool')`. The MedGemma path writes `inference`. Align the CHECK before relying on it in production.

### 1.5 Paystack tables (top-up lane, not this flow)

| Table | Utility |
|-------|---------|
| `billing.payment_intent` | Checkout / STK intent: amount, credits to grant, status. |
| `billing.paystack_event` | Webhook inbox with `dedupe_key`. |
| `billing.price_list` | Versioned **retail** job SKUs and settings (KES lane). Token rates for the meter lane live in `dermascan/billing/config.py`, not here. |

---

## 2. Flags and ceilings

Nothing bills unless a flag is on.

| Flag | Effect |
|------|--------|
| `BILLING_METER_ENABLED` | Write `usage_line` rows (shadow metering). |
| `BILLING_ENFORCE` | Place holds and block when the wallet cannot cover **hold + min floor**. |
| `billing_active()` | Either flag is on. |

`BILLING_ENFORCE=true` requires `BILLING_METER_ENABLED=true` (startup check). Enforce without meter would settle `actual=0` and refund the entire hold.

| Constant | Default | Role |
|----------|---------|------|
| `BILLING_ORCHESTRATOR_HOLD_CREDITS` | 150 | Orchestrator turn ceiling (~$1.50 API). |
| `BILLING_MEDGEMMA_HOLD_CREDITS` | 200 | MedGemma job ceiling (not the SKU price). |
| `BILLING_MIN_BALANCE_CREDITS` | 150 | Extra floor: `required = hold + min`. |
| `BILLING_SKIN_AUDIT_CREDITS` | 40 | Flat SKU charged on successful skin inference. |
| `BILLING_PRODUCT_REVIEW_IMAGE_CREDITS` | 50 | Flat SKU for product-image inference. |
| `BILLING_COMBINED_AUDIT_CREDITS` | 60 | Flat SKU for hybrid inference. |

A skin review that also runs the orchestrator therefore uses **two independent reservations** (turn hold + MedGemma hold), settled separately.

---

## 3. The three-phase loop

Same state machine for both scopes. Only the **key**, **hold size**, and **how usage is priced** differ.

```
place_hold          usage_line × N           commit_reservation
(reserve)     →     (accumulate)       →     (commit / refund)
ledger HOLD −H      no balance change        RELEASE +(H − actual)
reservation=held                             optional DEBIT if actual > H
                                             reservation=committed
```

### 3.1 Reserve (`place_hold`)

1. `SELECT account … FOR UPDATE`.
2. Reject if `balance < hold_credits + BILLING_MIN_BALANCE_CREDITS` → `InsufficientCreditsError`.
3. Insert `reservation` (`state=held`, `expires_at=now()+TTL`).
4. Ledger `kind=hold`, `delta_credits=-hold_credits`.
5. Wallet drops by the ceiling immediately. That credit is **escrow**, not the final bill.

If the key already exists, return the existing reservation (idempotent retries).

### 3.2 Accumulate (`usage_line`)

Each billable event writes a line. Balance does not change. Failed LLM fallbacks that never return usage are not written (v1: bill successful responses only).

### 3.3 Commit / refund (`commit_reservation`)

```
actual  = SUM(usage_line.cost_credits) for reservation_key
refund  = max(0, hold − actual)
overrun = max(0, actual − hold)
```

- `refund > 0` → ledger `release` `+refund` (unused escrow back to the wallet).
- `overrun > 0` → ledger `debit` `−overrun` (rare; hold was too small).
- Reservation → `committed`, `actual_credits=actual`.

Net wallet movement for the scope: **−actual** (hold − refund + overrun).

`release_reservation` is the cancel path (full hold returned, `actual=0`, `state=released`). Used by the **sweeper** on TTL expiry, not by the happy-path workers.

### 3.4 Stale holds

`billing/sweeper.py` periodically calls `expire_stale_holds`. Stuck `held` rows past `expires_at` get a full `release`. That is why a crashed worker does not keep the ceiling locked forever.

---

## 4. Orchestrator turn (token-priced)

**Key:** `request_id`  
**Job type:** `orchestrator_turn`  
**Hold:** `BILLING_ORCHESTRATOR_HOLD_CREDITS` (150)

### 4.1 Where it is wired

`orchestrator_worker._process_stream_message` (job stream only — **not** the signal / proactive path).

1. `get_or_create_account(channel, client_id)`.
2. If enforce: `place_hold(reservation_key=request_id)`. On `InsufficientCreditsError`, send the recharge message and ack the job (no agent invoke).
3. Set `BillingScope` on a `ContextVar` so model-chain hooks know `account_id`, `scope_key=request_id`, and `reservation_id` without threading IDs through LangGraph.
4. Run the agent.
5. **`finally`:** if a hold exists, **always `commit_reservation`**. Failed turns still settle: usage written before the crash is charged; unused hold is refunded. Then the scope is reset.

There is no separate “refund on exception” call. Commit *is* the refund.

### 4.2 How tokens become credits

Hook: `model_fallback_chain` after a successful invoke → `billing.meter.record_llm_usage`.

1. Read `AIMessage.usage_metadata` (`input_tokens`, `output_tokens`).
2. Map chain key (`gemini:gemini-3.5-flash`) → rate-card key (`google:gemini-3.5-flash`).
3. Purpose: `summarization` if the operation is summarization middleware; otherwise `agent_step`.
4. `credits_for_tokens` — each bucket rounded **up**:

   `credits = ceil(count × credits_per_1M / 1_000_000)`

5. `record_usage_line` with `meter_type=llm`, `line_seq` from `BillingScope.next_line_seq()`.

Only models on `BILLING_TOKEN_RATES` cost anything. Unknown models record `0` credits (still a line if tokens were extracted).

Typical turn: several `agent_step` lines (one per successful model call, including fallbacks that returned usage) plus optional `summarization` lines. A 3-step Gemini Flash turn is often a few credits against a 150 hold; settle refunds the rest.

### 4.3 Tools

Orchestrator tools (intake, Tavily, enqueue audit, …) are **not** billed as `meter_type=tool` SKUs today. `MeterType.TOOL` and the schema CHECK exist for a later Tavily / search line.

What *is* billed: the **LLM tokens** of the agent step that decided to call the tool and read the tool result. Tool I/O shows up as extra input on the next `agent_step`, not as a separate debit.

Dispatching MedGemma from a tool does **not** charge the orchestrator reservation for inference. That is a second hold on the MedGemma job id (section 5).

### 4.4 What is not on this reservation

- Signal / rules-engine / group-report LLM calls (`checkpoint_ns=orchestrator:signal`). No `BillingScope`, no hold.
- Outbound Telegram/WhatsApp delivery.

---

## 5. MedGemma (flat SKU)

**Key:** MedGemma `job_id`  
**Hold:** `BILLING_MEDGEMMA_HOLD_CREDITS` (200) — a **ceiling**, not the price.  
**Charge:** one flat SKU on **success only**.

MedGemma is self-hosted. There is no provider token invoice, so v1 does **not** meter GPU tokens. Config comments mention “flat + tokens”; the implementation is **flat only** (`input_tokens=0`, `output_tokens=0`).

### 5.1 SKU table

`resolve_medgemma_job_type(prompt)` (or an explicit override) → `credits_for_medgemma_job`:

| Job type | Default credits | Prompt hints |
|----------|-----------------|--------------|
| `skin_audit` | 40 | Default / skin review |
| `product_review_image` | 50 | “product review”, “ingredient list”, “product label” |
| `combined_audit` | 60 | “hybrid review”, “skin and product” |

These are env-overridable (`BILLING_SKIN_AUDIT_CREDITS`, …). They are **not** the Paystack `price_list` job SKUs (those are retail KES). Same names, different units and tables.

### 5.2 Reserve at enqueue

`gateways/endpoints.py` → `prepare_medgemma_job_billing` **before** the job is `XADD`’d:

1. Skip if billing is off or `channel` / `client_id` missing.
2. `get_or_create_account`.
3. Infer `job_type` from the audit prompt.
4. If enforce: `place_hold(reservation_key=job_id, hold_credits=200, job_type=…)`.
5. Persist `billing_account_id`, `billing_job_type`, `billing_reservation_id` on the Redis job so the worker can settle after a restart.

Insufficient credits at this gate is a **402** on the gateway. The orchestrator enqueue tool treats that as `insufficient_credits` and must **not** mark intake `AWAITING_AUDIT`.

### 5.3 Accumulate + settle on the worker

`worker._settle_job_billing` → `settle_medgemma_job`:

| Inference | Usage line | Then |
|-----------|------------|------|
| Success | One `meter_type=inference` line, `cost_credits=SKU`, `line_seq=1` | `commit_reservation` → charge SKU, refund `200 − SKU` |
| Failure | No usage line | `commit_reservation` → `actual=0`, full hold released |

Same commit function as the orchestrator. Failure is a full refund via settle, not `release_reservation`.

Example (skin audit, enforce on, hold 200, SKU 40):

```
hold     −200
usage    inference / skin_audit / 40   (balance unchanged)
release  +160
net      −40
```

---

## 6. End-to-end: one skin review

User sends a photo. Two wallets moves on the **same** `account`, different keys.

```
orchestrator:jobs          request_id = R
  hold  −150   reservation_key = R
  usage llm × N  (agent steps + tools-as-tokens)
  commit refund unused

  tool enqueues MedGemma job J
    hold  −200   reservation_key = J
    … inference …
    usage inference 40
    commit release 160
```

Ledger for that account will show two `hold` rows and two `release` rows (plus any grant/top-up). That is expected, not double-billing of the same scope.

---

## 7. Worked numbers

**Orchestrator (Gemini 3.5 Flash, from the rate card)**  
3 steps × (4k in + 120 out) ≈ 1 credit/step ≈ **3 credits** against a 150 hold → settle refunds ~147.

**MedGemma skin**  
Hold 200, SKU 40 → user pays **40**, 160 returns.

**Wallet gate**  
To *start* an orchestrator turn under enforce: `balance >= 150 + 150 = 300`.  
To *enqueue* MedGemma: `balance >= 200 + 150 = 350` at that moment (after the orchestrator hold is already out of the wallet). Size holds and the min floor with that stacking in mind.

---

## 8. Code map

| Concern | Module |
|---------|--------|
| Flags | `dermascan/billing/flags.py` |
| Rate card, SKUs, enums | `dermascan/billing/config.py` |
| Wallet | `dermascan/billing/accounts.py` |
| Ledger writes | `dermascan/billing/ledger.py` |
| Hold / commit / release / sweeper expire | `dermascan/billing/reservations.py` |
| Usage lines | `dermascan/billing/usage.py` |
| Token extract + hook | `dermascan/billing/meter.py` |
| Turn ContextVar | `dermascan/billing/context.py` |
| MedGemma prepare / settle | `dermascan/billing/medgemma.py` |
| Orchestrator reserve / commit | `orchestrator_worker._process_stream_message` |
| LLM hook | `model_fallback_chain` → `record_llm_usage` |
| MedGemma reserve | `medgemma_service/gateways/endpoints.py` |
| MedGemma settle | `medgemma_service/worker.py` |
| TTL refund | `dermascan/billing/sweeper.py` |

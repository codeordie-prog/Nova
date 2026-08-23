# Billing Phase 0 — Locked Decisions

Status: **approved for Phase 1 implementation**  
Date: 2026-08-21  
Owners: billing workstream

Phase 0 defines *what* we bill, *who* pays, *how* holds/settles work, and *where* data lives.
No deduction hooks until Phase 4–5.

---

## 1. Accounting model

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Source of truth | **Cloud SQL** (`billing` schema) | Ledger-first; auditable; matches Paystack |
| Hot cache (v1) | **None** — SQL `SELECT … FOR UPDATE` only | Simpler; add Redis read cache in v2 if needed |
| Balance invariant | `account.balance_credits = SUM(ledger.delta_credits)` | Nightly verify; P1 on drift |
| Ledger mutability | **Append-only** — never UPDATE/DELETE `ledger` | Audit trail |
| Idempotency | **`ledger.idempotency_key` UNIQUE** on every balance mutation | Safe retries from workers |

---

## 2. Who pays (account identity)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary key | **`billing.account.id` (uuid)** | Stable wallet id |
| Lookup key | **`UNIQUE (channel, client_id)`** | Matches ingress (`whatsapp`, `telegram`) |
| `client_id` | Phone / Telegram user id from API | Already on every job |
| `owner_id` | **Deferred** — nullable uuid | Future: one human, multiple channels |
| Auto-create account | **Yes** on first billable ingress | Upsert `(channel, client_id)` → account row |
| Free tier | **`free_credits_granted` flag** + one-time `ledger.kind=grant` | Onboarding credits once per account |

**Not** billing on `case_id` — cases are clinical units; wallet is per messaging identity.

---

## 3. Billable scopes (reservation keys)

| Scope | `reservation_key` | When hold | When settle |
|-------|-------------------|-----------|-------------|
| Orchestrator turn | `request_id` | Job stream message start | `agent.invoke` complete (success or fail) |
| MedGemma job | `medgemma_job_id` | Gateway enqueue / worker dispatch | Inference complete |
| Grouped media trip | `group_id` (optional wrapper) | When combined review starts | All required jobs settled |

v1 implements **orchestrator turn only** (`request_id`). MedGemma in Phase 7.

---

## 4. Credit unit

| Decision | Choice |
|----------|--------|
| Storage type | **`bigint` credits** (SQL + code) |
| User-facing currency | **KES** on account / Paystack |
| Internal sub-unit | **1 credit = 1 user credit** for v1 (no microcredits) |
| Token → credits | Rate card converts tokens to **integer credits** (round up at line level) |

If fractional costs matter later, switch to millicredits (`×1000`) without changing schema shape.

---

## 5. Hold → meter → settle

### 5.1 Hold (pre-budget)

- At orchestrator turn **start**: create `reservation` (`state=held`, `credits=CEILING`, `expires_at=now()+TTL`).
- Same transaction: `ledger` row **`kind=hold`**, `delta_credits=-CEILING`, `ref_type=reservation`, `ref_id=reservation.id`.
- Update `account.balance_credits` and `balance_after` on ledger row.
- If `balance_credits < CEILING` after prior holds → **reject turn** with user-facing recharge message.

### 5.2 Meter (during turn)

- Each LLM call: record **`usage_line`** (Phase 1 table) or `ledger.metadata.lines[]` (v1 acceptable).
- Fields: `provider`, `model`, `input_tokens`, `output_tokens`, `purpose` (`agent_step` | `summarization`).
- Optional v2: flat `tool.tavily_search` per call.
- **No balance change** during metering.

### 5.3 Settle (end of turn)

- `actual_credits = sum(usage lines priced via rate card)`, capped at `CEILING` unless overrun policy applies.
- `refund = CEILING - actual_credits`
- One transaction:
  - If `refund > 0`: `ledger.kind=release`, `delta_credits=+refund`
  - If `actual > CEILING` (overrun): extra `ledger.kind=debit` for delta (should be rare)
  - `reservation.state=committed`, `settled_at=now()`
- Store token breakdown in settlement metadata for support.

### 5.4 Expired holds

- Sweeper job: `held` + `expires_at < now()` → `state=released`, full `ledger.kind=release` refund of held amount.
- TTL default: **15 minutes** (configurable).

---

## 6. Ledger `kind` enum (locked)

| kind | Meaning | delta sign |
|------|---------|------------|
| `grant` | Free / promo credits | + |
| `topup` | Paystack payment | + |
| `hold` | Reservation placed | − |
| `release` | Unused hold returned | + |
| `debit` | Final usage charge (overrun or non-reserved) | − |
| `refund` | Payment reversal | +/− |
| `adjustment` | Manual ops | +/− |

**Rule:** one `hold` per reservation; one `release` for unused portion; optional `debit` only on overrun.

---

## 7. LLM metering policy

| Decision | Choice |
|----------|--------|
| Token source | `AIMessage.usage_metadata` (`input_tokens`, `output_tokens`) |
| Hook point | `ModelChainRunner._invoke_chain_once` after successful handler |
| Summarization | Same hook via `invoke_chat`; tag `purpose=summarization` |
| Failed fallback attempts | **Bill only successful LLM responses** that return usage (v1) |
| Providers in scope | `openai:*`, `gemini:*` on orchestrator chain |

---

## 8. Rate card (v1) — locked 2026-08-21

Stored in `dermascan/shared/billing_config.py` and `cloud_sql/billing/RATE_CARD.md`.

**Scale:** `100 credits = $1.00 USD` of provider API list price (`BILLING_CREDITS_PER_USD`).

| Model key | USD in / out (per 1M) | Credits in / out (per 1M) |
|-----------|----------------------|----------------------------|
| `google:gemini-3.5-flash` (primary) | $1.50 / $9.00 | 150 / 900 |
| `google:gemini-3.1-flash-lite` | $0.25 / $1.50 | 25 / 150 |
| `openai:gpt-4o` | $2.50 / $10.00 | 250 / 1000 |
| `openai:gpt-5` | $1.25 / $10.00 | 125 / 1000 |
| `openai:gpt-4o-mini` (summarization) | $0.15 / $0.60 | 15 / 60 |

Sources: [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) (Aug 2026).

**Hold ceilings (defaults — tune from LangSmith p95):**

| Scope | Env var | Default |
|-------|---------|---------|
| Orchestrator turn | `BILLING_ORCHESTRATOR_HOLD_CREDITS` | `150` |
| MedGemma job | `BILLING_MEDGEMMA_HOLD_CREDITS` | `200` |
| Min balance to start turn | `BILLING_MIN_BALANCE_CREDITS` | `150` |

---

## 9. User-facing pricing (v1)

| Decision | Choice |
|----------|--------|
| Customer UX | **Credits balance** (integer); KES only at top-up via Paystack |
| Pass-through vs SKU | **Internal token-accurate ledger**; marketing SKUs later (`job_type` on reservation) |
| Insufficient credits message | Fixed copy via outbound; do not invoke orchestrator agent |

---

## 10. Payment (Phase 8 — schema reserved now)

- Paystack → `payment_intent` + `paystack_event` (colleague schema).
- Successful payment → `ledger.kind=topup` + `account.balance_credits` in one transaction.
- Email collected on first top-up.

---

## 11. Observability

| Role | Tool |
|------|------|
| Debug / reconcile tokens | LangSmith (`metadata.request_id`) |
| Local turn summary | `agent_call_tracer` (extend with token totals later) |
| Accounting audit | SQL `ledger` + nightly invariant job |

LangSmith is **not** the billing wallet.

---

## 12. Out of scope for v1

- Redis balance cache
- Escalation billing (`escalation` meter — separate SKU later)
- Pinecone / Tavily flat fees (Phase 7+)
- `owner_id` cross-channel wallet merge
- Real-time balance UI (read from SQL account row)

---

## 13. Schema approval

Phase 1 applies unified schema (`cloud_sql/billing/schema.sql`) supporting **both**:

1. **Meter/hold lane** — `reservation`, `usage_line`, ledger `hold`/`release`
2. **Paystack lane** — `payment_intent`, `paystack_event`, `price_list`, `account.msisdn`

See `cloud_sql/billing/SCHEMA_ALIGNMENT.md` for lane boundaries.

Existing DBs that already ran an earlier `schema.sql`: apply `002_align_paystack_schema.sql`.

**Ledger:** no CHECK on `kind`/`ref_type` — superset enums for both lanes.

---

## 14. Implementation order (reminder)

1. Phase 1 — SQL schema + migrations  
2. Phase 2 — `billing_config.py` rate card + ceilings  
3. Phase 3 — account resolver `(channel, client_id) → account_id`  
4. Phase 4 — metering hooks (emit-only)  
5. Phase 5 — reserve / settle in orchestrator worker  
6. Phase 6 — async usage_line flush if not sync  
7. Phase 7 — MedGemma reservation  
8. Phase 8 — Paystack  

---

## 15. Open items (fill before Paystack go-live)

- [ ] Finance: retail KES ↔ credits at top-up (e.g. 100 KES = N credits, with markup over cost base)
- [ ] Product: free grant amount on first account (`BILLING_FREE_GRANT_CREDITS`)
- [ ] Product: exact insufficient-credits user message copy
- [ ] Eng: Cloud SQL connection env vars in deployment (reuse `qutis_db` / `billing` schema)
- [x] Token rate card for orchestrator chain models (see `RATE_CARD.md`)

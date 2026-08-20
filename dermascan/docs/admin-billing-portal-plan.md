# Admin Billing & Usage Portal — Plan

Internal portal for tracking **what each job cost us** (tokens, GPU seconds) against **what the user
was billed** (credits), per user and per job type. Companion to
`dermascan/docs/billing-credits-plan.md`.

**Status: plan only. Nothing built.**

---

## 1. What exists today (verified in code)

| Thing | Reality |
|---|---|
| Token capture | **None.** No `usage_metadata` / `input_tokens` / `output_tokens` read anywhere in the repo |
| `shared/agent_call_tracer.py` | Tracks model *calls* per turn: `model_calls`, `provider_attempts`, `fallback_count`, `tool_names`, keyed by `client_id` / `request_id` / `case_id` / `channel` / `flow` |
| Where that goes | A **rotating log file** (`RotatingFileHandler`, `propagate=False`). Ephemeral, not queryable, lost on container restart |
| LangSmith | Enabled (`LANGSMITH_*`), but it is a vendor trace UI — per-run debugging, not per-user billing aggregation. Not something to invoice from |
| MedGemma cost | Not measured at all |
| Existing dashboard | `/Projects/qutis-escalation` — Next.js 15.5, Prisma + Postgres, shadcn/Radix, NextAuth `auth()` middleware, routes `queue` / `my-cases` / `closed` / `audit` |
| Dashboard auth | **Physician-only.** `middleware.ts` gates every route on `req.auth?.user?.physicianId`. No role concept exists |

The good news: `agent_call_tracer` already carries the exact attribution context the portal needs
(`client_id`, `request_id`, `case_id`, `flow`) and already fires exactly once per turn via
`end_turn()`. It is the right place to hang token accounting — it just needs numbers and a durable
sink instead of a log file.

---

## 2. Two meters, deliberately separate

The single most important idea in this plan:

| Meter | Unit | Nature | Owner |
|---|---|---|---|
| **Credits** | KES-denominated, deterministic | What the user is billed. 20 credits for a skin audit, always | `billing-credits-plan.md` |
| **Usage** | tokens, GPU seconds | What the job actually cost us. Varies wildly per user | this doc |

A skin audit is always 20 credits. But one user's audit is 3k tokens and one clean MedGemma pass;
another's is 15k tokens across a long intake with two model fallbacks and a checkpoint-reset retry.
Same price, 5× the cost.

**The portal exists to make that gap visible.** Not to bill from it — billing stays deterministic, because
users cannot reason about token-priced products. Usage data drives *your* pricing decisions, not the
invoice.

---

## 3. Phase A — token metering in Nova (prerequisite)

Nothing in the portal is possible until usage is captured. This is the real work; the UI is the easy
part.

### 3.1 Capture point

`ModelChainRunner` in `backend/services/agents/model_fallback_chain.py` is the single choke point —
every model call in the system goes through it:

| Site | Line | Returns |
|---|---|---|
| `_invoke_chat_once` | :973 | `AIMessage` |
| `_invoke_structured_once` | :919 | structured response |
| `_invoke_agents_once` | :1075 | agent result dict with `messages[]` |

LangChain exposes `usage_metadata` on `AIMessage` (`input_tokens`, `output_tokens`, `total_tokens`)
across both `langchain-openai` and `langchain-google-genai`, plus provider extras in
`response_metadata` (cached tokens, reasoning tokens on gpt-5/o-series). For agent invokes, sum
`usage_metadata` across every `AIMessage` in the returned `messages` list — a single agent turn is
many model calls.

**Attribution comes free.** These run inside the `agent_call_tracer` context var set by
`start_turn()`. Extend `TurnTrace` with token accumulators, add
`record_model_usage(provider, model, input_tokens, output_tokens, …)` alongside the existing
`record_model_attempt()`, and flush in `end_turn()`.

### 3.2 MedGemma — measure GPU seconds, not tokens

MedGemma runs on Modal L4 via vLLM and reports no token stream back to Nova. Tokens are also the
wrong unit there: the cost driver is **GPU wall time**, not tokens generated.

The worker knows job start and end. Emit a usage event with `provider='modal'`, `gpu_seconds`, and
cost = `gpu_seconds × MODAL_L4_USD_PER_SECOND`. That single number is ~60–80% of the true cost of a
skin audit, so the portal is misleading without it.

### 3.3 Schema (Nova Postgres, `billing` schema)

```sql
CREATE TABLE billing.usage_event (
  id             bigserial PRIMARY KEY,
  account_id     uuid REFERENCES billing.account(id),
  client_id      text NOT NULL,
  request_id     text NOT NULL,
  case_id        text,
  channel        text,
  flow           text,                    -- job | signal
  job_type       text,                    -- correlates to the billed deliverable
  provider       text NOT NULL,           -- openai | gemini | modal | tavily
  model          text NOT NULL,
  input_tokens   bigint NOT NULL DEFAULT 0,
  output_tokens  bigint NOT NULL DEFAULT 0,
  cached_tokens  bigint NOT NULL DEFAULT 0,
  reasoning_tokens bigint NOT NULL DEFAULT 0,
  gpu_seconds    numeric(10,3) NOT NULL DEFAULT 0,
  model_calls    int NOT NULL DEFAULT 1,
  fallback_count int NOT NULL DEFAULT 0,
  tool_names     text[],
  cost_usd       numeric(12,6) NOT NULL DEFAULT 0,   -- computed at write time
  duration_ms    int,
  status         text NOT NULL,           -- ok | failed
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON billing.usage_event (account_id, created_at DESC);
CREATE INDEX ON billing.usage_event (request_id);
CREATE INDEX ON billing.usage_event (created_at DESC, job_type);

-- Versioned so historical cost is never rewritten when a vendor changes prices.
CREATE TABLE billing.model_price (
  id                  bigserial PRIMARY KEY,
  provider            text NOT NULL,
  model               text NOT NULL,
  input_per_1m_usd    numeric(10,4),
  output_per_1m_usd   numeric(10,4),
  per_second_usd      numeric(10,6),      -- Modal GPU
  effective_from      timestamptz NOT NULL,
  UNIQUE (provider, model, effective_from)
);
```

`cost_usd` is computed and frozen at write time against the price row in effect. Never recompute
historical cost from current prices — margin history becomes fiction.

### 3.4 Write path — never block a user turn

```
end_turn() → XADD billing:usage  (Redis, fire-and-forget)
           → billing_worker batches ~100 rows / 5s → Postgres COPY
```

Telemetry must never fail a request. Redis unavailable → log and drop. A missing usage row is an
analytics gap; a failed skin audit is a lost user.

Flag: `USAGE_METERING_ENABLED` (default off), independent of `BILLING_ENABLED` — usage metering is
worth running **before** billing goes live, since it is exactly what tells you whether the price list
is right.

---

## 4. Phase B — where the portal lives

**Extend `/Projects/qutis-escalation`.** It already has Next.js 15, Prisma, shadcn/Radix, an auth
layer, and a deploy path. Building a second dashboard would duplicate all of it.

New route group `app/(admin)/`, sibling to the existing `(app)/`.

### 4.1 The access-control gap — the one real security change

`middleware.ts` currently gates everything on `req.auth?.user?.physicianId`. **Every authenticated
user is a physician**, and there is no role concept.

Dermatologists must not see revenue, user balances, or phone-number-level billing. This needs:

- `role` on `Physician` (or a separate `AdminUser` model): `physician | admin | owner`
- middleware: `/admin/*` and `/api/admin/*` require `role IN ('admin','owner')`
- physicians' existing access unchanged — additive only
- role changes written to the existing `AuditEvent` table

Treat this as its own reviewable step. It is the only part of the portal that can leak data if it is
wrong.

### 4.2 Data access — Nova API, not a second Prisma datasource

Billing tables live in **Nova's** Postgres (`qutis_db`, via `POSTGRES_*`). The dashboard has its own
`DATABASE_URL`. Two ways to bridge:

| Option | Verdict |
|---|---|
| (a) Second Prisma datasource pointed at `qutis_db` | Faster to build. But couples the dashboard's schema to Nova's billing schema — every Nova migration becomes a dashboard migration, and two services now write-lock the same tables |
| (b) **Nova exposes read-only `/v1/admin/billing/*`, HMAC-signed** | **Recommended.** Matches the existing Nova↔dashboard pattern (`shared/hmac_auth.py`, `/v1/webhooks/escalation`). Nova stays the single owner of billing data. Portal is a pure consumer. Survives the Cloud Run split in `cloud-deployment-plan.md` |

Reuse `shared/hmac_auth.py` as-is here — SHA256 over `{ts}.{body}`, `X-Qutis-Signature`, 300s replay
window. (Unlike the Paystack webhook, which needs its own SHA512 verifier.)

### 4.3 Endpoints

```
GET  /v1/admin/billing/overview?from&to               totals, margin, trend
GET  /v1/admin/billing/users?sort&cursor&limit        per-user table
GET  /v1/admin/billing/users/{account_id}             balance, lifetime spend, cost, margin
GET  /v1/admin/billing/users/{account_id}/ledger      paginated credit ledger
GET  /v1/admin/billing/users/{account_id}/usage       paginated usage events
GET  /v1/admin/billing/usage?group_by=job_type|model|day
GET  /v1/admin/billing/payments?status                Paystack intents + reconciliation state
POST /v1/admin/billing/accounts/{id}/adjust           manual grant / deduction
```

`adjust` writes a `billing.ledger` row with `kind='adjustment'` and the acting admin in `metadata`.
**Never mutate `balance_credits` directly** — the append-only ledger is the audit trail, and support
credits are exactly the case where you will later need to prove what happened.

---

## 5. Screens

| Route | Content |
|---|---|
| `/admin` | Revenue (KES), credits sold vs consumed, AI cost, **gross margin**, top-ups today, active users, failed payments, held-reservation count |
| `/admin/users` | Table: client_id (masked), channel, balance, credits consumed, tokens, est. cost, **margin**, last active. Sortable by cost and by margin |
| `/admin/users/[id]` | Per-request timeline: ledger entries, usage events, reservations held/committed/released, payment history |
| `/admin/usage` | Tokens by model/provider/day, fallback rate, cost per job type, p50/p95 cost distribution |
| `/admin/payments` | Paystack intents, status, reconciliation mismatches, stuck `pending` M-PESA |
| `/admin/pricing` | Current credit price list + model price table (read-only v1) |

### The chart that matters

**Cost distribution per job type (p50 / p95 / max) against the credit price line.** Averages hide the
problem; a job type is unprofitable when its *tail* crosses the price, not its mean. Everything else
on the overview screen is decoration by comparison.

---

## 6. Margin math to surface

```
revenue_kes  = credits_consumed × 1.00          (1 credit = KES 1)
cost_kes     = cost_usd × USD_KES_RATE
margin_kes   = revenue_kes − cost_kes
margin_pct   = margin_kes / revenue_kes
```

Per job type, per user, per day. Two alerts worth wiring:

- **p95 cost > price** for any job type → pricing is wrong for the tail
- **negative lifetime margin** on a user account → abuse, a stuck retry loop, or escalation-heavy usage

On escalation specifically: at KES 40 with a per-case derm payout, `specialist_escalation` margin is
expected to be **negative by design**. Break it out on its own line so it never silently drags the
blended number without anyone noticing how big it has grown.

---

## 7. Privacy

The portal shows `client_id` — which is a **phone number** on WhatsApp. That is PII, and this is
health-adjacent.

- Mask by default (`+2547•••123`); reveal is an explicit click, written to `AuditEvent`
- **Never** render conversation content, skin images, or diagnoses in the billing portal. Clinical
  content belongs to the escalation dashboard, under its own consent model
- Admin ≠ physician, in both directions
- Retain raw `usage_event` rows 90 days, then roll up to daily aggregates and drop the detail

---

## 8. Phases

| Phase | Scope | Depends on |
|---|---|---|
| A | Token metering: `usage_event` + `model_price` + `ModelChainRunner` capture + `billing:usage` stream + worker sink | — |
| A2 | MedGemma GPU-seconds reporting from the Modal worker | A |
| B | Nova read-only admin API, HMAC-signed | A, billing phase 0 |
| C | Role split + `/admin` gating in qutis-escalation | — (can run parallel) |
| D | Screens: overview, users, user detail | B, C |
| E | Usage analytics, cost distribution, margin alerts | D |
| F | Payments/reconciliation view + manual adjustments | D, billing phase 2 |

**A is worth doing on its own, before any billing is enforced.** Two weeks of real usage data is what
turns the price list in `billing-credits-plan.md` from an estimate into a decision — and it pairs
exactly with billing shadow mode (`BILLING_ENABLED=true`, `BILLING_ENFORCE=false`), which records
what *would* have been charged. Run both together and the portal shows real margin before a single
user is blocked.

---

## 9. Open questions

1. **Admin identity.** Reuse `Physician` with a `role` column, or a separate `AdminUser` model? Separate
   is cleaner conceptually; reuse is less work and keeps one login flow.
2. **USD→KES rate** for cost display: fixed config value, or fetched daily? Fixed is fine at pilot
   scale and avoids a runtime dependency on an FX source.
3. **Portal deployment.** Same Cloud Run service as the escalation dashboard, or separate? Same is
   simpler; separate lets you restrict admin access by IP.
4. **Tavily / search costs** — worth metering as usage events, or fold into a flat overhead per job
   type? They are small but non-zero.

---

## Related docs

- `dermascan/docs/billing-credits-plan.md` — credits, pricing, Paystack, reserve/commit ledger
- `dermascan/docs/cloud-deployment-plan.md` — Cloud Run services, Secret Manager
- `/Projects/qutis-escalation` — the dashboard this extends

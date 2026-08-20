# Billing & Credits Plan (Paystack)

Prepaid **credit wallet** for Nova. Users top up via Paystack (M-PESA or card, KES 100 minimum),
AI deliverables consume credits. Ships behind `BILLING_ENABLED` with a shadow mode so pricing can be
validated on real traffic before anything blocks.

---

## 1. What the flow looks like today (billable surface)

Traced end to end before designing. There is **no existing billing, payment, or account code** in the
repo — this is greenfield.

```
Telegram / WhatsApp webhook              endpoints.py:821 / :910
  → _enforce_ingress_capacity()          global RPM + queue depth  (no per-user notion)
  → client_id = phone (WA) | from.id (TG)
  → XADD orchestrator:jobs               endpoints.py:666

orchestrator_worker.py:1588 _process_stream_message
  → per-client lock, LangGraph invoke (Gemini 3.5 flash → OpenAI chain)
  → tools: update_intake, register_intake_image, enque_request_for_auditing_stream,
           read_status_of_a_case, internet_search, request_specialist_escalation …
  → reply → _enqueue_outbound → outbound:messages → outbound_sender_worker

enque_request_for_auditing_stream                    orchestrator_tools.py:2157
  → _dispatch_audit_image_ref                        orchestrator_tools.py:1767
  → POST {MEDGEMMA_GATEWAY_URL}/audit_image_ref
  → medgemma:jobs → Modal L4 GPU

MedGemma completes → XADD orchestrator:signals
orchestrator_worker.py:1970 _process_signal_message
  → signal_type product_review_* → run_product_signal_rules_flow    (ingredient LLM + CIR rules + report LLM)
  → signal_type diagnose_*/skin_* → run_skin_signal_rules_flow      (skin review LLM + Tavily)
  → escalation signals → escalation:jobs → human dermatologist
  → reply → _enqueue_outbound
```

**`request_id` is the correlation key end to end** — it is present in `orchestrator:jobs` fields, in the
MedGemma dispatch payload, and back in `orchestrator:signals` fields. That makes it the natural
reservation and idempotency key. `group_id` supersedes it for Telegram albums (one report per group).

### Unit economics (what each job actually costs us)

| Unit | Work performed | Rough marginal cost |
|---|---|---|
| Chat turn | 1 LangGraph invoke, Gemini flash, ~2–8k tokens | ~$0.001 |
| Skin audit | MedGemma L4 GPU ~15s + skin review LLM + Tavily | ~$0.015 |
| Product review (image) | MedGemma label vision + ingredient LLM + Pinecone/Postgres CIR + report LLM | ~$0.025 |
| Product review (text) | ingredient LLM + CIR + report LLM, **no GPU** | ~$0.010 |
| Deep research | Tavily + knowledge LLM | ~$0.005 |
| Specialist escalation | **Human dermatologist** | real KES cost — dominates everything |

---

## 2. Recommendation: bill deliverables, not chat turns

You asked for "each job handled by the AI consumes credits." The design supports that, but the
default I recommend is **chat turns cost 0 credits**, and only the deliverables bill. Reasons:

1. **The intake flow is multi-turn by design.** The cosmetic pivot deliberately requires profile,
   medications context, and a skin snapshot *before* an audit is enqueued
   (`MISSING_MEDICATIONS_CONTEXT`, `_ready_for_audit_enqueue`). Charging per turn bills the user for
   the system's own gating.
2. **"hi" / "thanks" / "what does that mean?"** would each cost money. That reads as hostile and
   suppresses the clarifying questions that improve audit quality.
3. **Chat is ~5% of an audit's cost.** Fold it into the audit price instead of metering it.
4. **Abuse is already handled elsewhere** — `_enforce_global_rate_limit` and the queue-depth gate.

`BILLING_CHARGE_CHAT_TURNS=true` flips this if you disagree; the ledger supports a `chat_turn` job
type either way.

---

## 3. Credit model

**1 credit = KES 1.00.** Minimum top-up KES 100 = 100 credits. Credits are the invariant unit;
currency prices are set explicitly per currency, never derived from runtime FX (FX drift and rounding
are an arbitrage surface).

### Price list v1

| Job type | Credits | KES | Per KES 100 |
|---|---|---|---|
| `chat_turn` | 0 | — | unlimited (rate-limited) |
| `skin_audit` | 20 | 20 | 5 |
| `product_review_image` | 25 | 25 | 4 |
| `product_review_text` | 15 | 15 | 6 |
| `deep_research` | 5 | 5 | 20 |
| `specialist_escalation` | 40 | 40 | 2.5 |

Margins are ~10× marginal cost on AI jobs, which covers Modal idle time, Redis Cloud, Postgres, and
support.

`specialist_escalation` at KES 40 is set deliberately low — it is the conversion moment for the
Qutis derm network, not a margin line. **It is the one price that can go negative:** if a
dermatologist is paid per case at more than KES 40, every escalation loses money. That is a fine
trade for a pilot (it buys derm-network liquidity and real clinical feedback), but the admin portal
must surface escalation margin separately so the loss stays visible and bounded. See
`dermascan/docs/admin-billing-portal-plan.md`.

### Top-up tiers

| Currency | Minimum | Credits | Bonus tiers |
|---|---|---|---|
| KES | 100 | 100 | 500 → 550, 1000 → 1150 |
| NGN | 2,000 | 100 | proportional |
| GHS | 15 | 100 | proportional |
| ZAR | 20 | 100 | proportional |
| USD | 1 | 100 | proportional |

Non-KES tiers are only reachable if your Paystack account is enabled for them — Kenya-only accounts
settle KES.

### Free trial

40 credits granted once per account on first contact (2 skin audits). Onboarding must never hit a
paywall before the user has seen the product work. Tracked by `free_credits_granted` so it cannot be
farmed by re-messaging.

---

## 4. Reserve → commit / release

The core mechanic. **Jobs fail** — MedGemma returns 429 `under_pressure`, model chains exhaust,
messages land in the DLQ. A user must never lose credits for work that was never delivered.

```
dispatch time                  delivery time
─────────────                  ─────────────
reserve(request_id, cost)  ──► commit(request_id)      report delivered  → credits gone
   balance ≥ cost?             release(request_id)     job failed / 429  → credits back
   state = held                (TTL sweeper)           stuck > 30 min    → credits back
```

- **Available balance** = `balance_credits − SUM(held reservations)`. This is what the gate checks.
- **Reservation key** = `group_id` when present, else `request_id`. Unique — makes reserve idempotent
  under stream redelivery and the reclaim loop.
- **TTL** = 30 min, swept by a background loop. Covers the case where a MedGemma job never returns a
  signal at all.

### Gate points

| # | Where | Action |
|---|---|---|
| G1 | `orchestrator_tools.py:1767` `_dispatch_audit_image_ref` | **reserve** before the HTTP POST; release on 429 / exception |
| G2 | `orchestrator_tools.py:2475` `continue_product_review_after_skin_prerequisite` | **separate reserve** — see below |
| G3 | `orchestrator_tools.py:3310` `request_specialist_escalation` | **reserve** escalation credits |
| G4 | `orchestrator_worker.py:2047–2085` `_process_signal_message` | **commit** after `_enqueue_outbound` succeeds |
| G5 | `orchestrator_worker.py:1761` job `except` | **release** all held reservations for the request |
| G6 | `text_product_review.py` | reserve/commit for the text-only path |

### Edge cases that will bite

- **Auto-chained product review (G2).** After a skin report is delivered,
  `continue_product_review_after_skin_prerequisite` dispatches a *second* billable job the user did
  not ask for in that turn. It needs its own reserve. If the balance covers the skin audit but not the
  product review, deliver the skin report and tell the user they need credits for the product
  review — do not fail both.
- **Telegram albums.** `_flush_telegram_media_group_after_window` enqueues N rows for one logical
  report compiled by `group_report_aggregator`. Reserve **once** on `group_id`, commit once when the
  aggregator delivers.
- **`under_pressure` (429).** `_dispatch_audit_image_ref` returns a soft status, not an exception.
  Release explicitly on that branch or credits leak into held state until TTL.
- **Physician-reviewed signals.** `*_physician_reviewed` arrives long after the escalation was
  charged. It is a *result* of an already-billed job — never charge again.
- **Checkpoint reset retries.** `_invoke_agent_with_recursion_retry` re-invokes the agent, which can
  re-call a tool. Reservation-key uniqueness makes the second reserve a no-op instead of a double
  charge.

---

## 5. Storage

**Postgres is authoritative. Redis is a cache.** Money does not live in a cache that can be flushed.

The gate only fires on expensive jobs (an audit is already 15s+ of GPU), so one ~50ms Postgres
round-trip on the dispatch path is negligible. Redis holds only the read-through balance for display
and low-balance nudges.

Reuse the existing `POSTGRES_*` connection settings (`rules_engine.py:120`), separate schema `billing`.

```sql
CREATE TABLE billing.account (
  id                   uuid PRIMARY KEY,
  channel              text NOT NULL,              -- whatsapp | telegram
  client_id            text NOT NULL,              -- +2547… | telegram user id
  owner_id             uuid,                       -- links channels to one human, later
  email                text,                       -- required by Paystack; collected at first top-up
  currency             char(3) NOT NULL DEFAULT 'KES',
  balance_credits      bigint NOT NULL DEFAULT 0,
  free_credits_granted boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, client_id)
);

-- Append-only. Never UPDATE or DELETE.
CREATE TABLE billing.ledger (
  id              bigserial PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES billing.account(id),
  delta_credits   bigint NOT NULL,                 -- +topup/grant, -debit
  balance_after   bigint NOT NULL,
  kind            text NOT NULL,                   -- topup|debit|release|grant|refund|adjustment
  job_type        text,                            -- skin_audit | product_review_image | …
  ref_type        text NOT NULL,                   -- reservation|payment|manual
  ref_id          text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  metadata        jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE billing.reservation (
  id              uuid PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES billing.account(id),
  reservation_key text NOT NULL UNIQUE,            -- group_id or request_id
  job_type        text NOT NULL,
  credits         bigint NOT NULL,
  state           text NOT NULL,                   -- held|committed|released
  case_id         text, client_id text, channel text,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  settled_at      timestamptz
);
CREATE INDEX ON billing.reservation (state, expires_at) WHERE state = 'held';

CREATE TABLE billing.payment_intent (
  id                 uuid PRIMARY KEY,
  account_id         uuid NOT NULL REFERENCES billing.account(id),
  reference          text NOT NULL UNIQUE,         -- ours, sent to Paystack
  amount_minor       bigint NOT NULL,              -- KES 100 -> 10000
  currency           char(3) NOT NULL,
  credits_to_grant   bigint NOT NULL,
  status             text NOT NULL,                -- initialized|pending|success|failed|abandoned
  channel_used       text,                         -- mobile_money|card
  authorization_url  text,
  raw                jsonb NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE billing.paystack_event (
  id          bigserial PRIMARY KEY,
  dedupe_key  text NOT NULL UNIQUE,                -- event + reference + amount
  event       text NOT NULL,
  reference   text,
  payload     jsonb NOT NULL,
  status      text NOT NULL,                       -- received|processed|ignored|error
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);
```

**Invariant:** `account.balance_credits = SUM(ledger.delta_credits)` for that account. Verified
nightly; any drift is a P1.

All balance mutations run in one transaction with `SELECT … FOR UPDATE` on the account row.

---

## 6. Paystack integration

Verified against Paystack's current docs (Aug 2026). Base URL `https://api.paystack.co`,
`Authorization: Bearer {PAYSTACK_SECRET_KEY}`.

### 6.1 Two top-up paths

**M-PESA STK push — WhatsApp users (phone already known).** One step, no browser.

```http
POST /charge
{
  "email": "user@qutis.app",
  "amount": 10000,                       // KES 100 in SUBUNITS (×100)
  "currency": "KES",
  "reference": "qutis_{acct8}_{ulid}",
  "mobile_money": { "phone": "+254722000000", "provider": "mpesa" },
  "metadata": { "account_id": "…", "client_id": "…", "channel": "whatsapp", "credits": 100 }
}
```
Responds `status: "pay_offline"` — the user gets an STK prompt and enters their PIN. Completion
arrives by webhook.

**Hosted checkout — Telegram users (no phone).**

```http
POST /transaction/initialize
{
  "email": "…", "amount": 10000, "currency": "KES",
  "reference": "qutis_{acct8}_{ulid}",
  "channels": ["mobile_money", "card"],
  "callback_url": "https://…/billing/return",
  "metadata": { … }
}
```
Returns `data.authorization_url` → send as a link in chat.

M-PESA limits (Safaricom, not Paystack): **KES 250,000 per transaction, KES 500,000 per day.**

### 6.2 Webhook — `POST /v1/webhooks/paystack`

```
1. Read the RAW request body — bytes, never a re-serialized dict.
2. hmac.new(PAYSTACK_SECRET_KEY, raw_body, hashlib.sha512).hexdigest()
   compare_digest against header  x-paystack-signature      ← SHA512, not SHA256
3. Optional IP allowlist: 52.31.139.75, 52.49.173.169, 52.214.14.220
4. INSERT billing.paystack_event ON CONFLICT (dedupe_key) DO NOTHING
   → already seen? return 200 now, do nothing else.
5. XADD billing:jobs  (async processing)
6. return 200 immediately.                ← non-200 triggers retries for 72h
```

Then the async processor:

```
7. GET /transaction/verify/{reference}          ← ALWAYS re-verify
8. assert data.status == "success"
       data.amount   == intent.amount_minor
       data.currency == intent.currency
9. ONE transaction: ledger INSERT (+credits) + account balance UPDATE + intent → success
10. _enqueue_outbound → "Top-up received. Balance: N credits."
```

Do **not** reuse `shared/hmac_auth.py` — that is SHA256 over `{ts}.{body}` with `X-Qutis-Signature`
for the escalation dashboard. Paystack is a different scheme (SHA512, raw body, no timestamp) and
needs its own verifier.

### 6.3 Gotchas that cost real money

| Gotcha | Consequence if missed |
|---|---|
| Amount is in **subunits** (×100) | charge 100× too little or too much |
| Signature is over **raw bytes** | re-serializing JSON changes byte order → every webhook rejected |
| **Always re-verify** before granting | Paystack's own docs warn about double-fulfilment for digital value |
| Webhooks fire **only on success** | pending M-PESA never resolves → needs the verify poller below |
| Must return **200 fast** | 72h of retries, duplicate processing |
| Dedupe on event+reference | Paystack legitimately re-sends webhooks |

### 6.4 Verify poller & reconciliation

- **Poller:** every 2 min, re-verify `payment_intent` rows in `initialized`/`pending` older than
  3 min, up to 30 min, then mark `abandoned`. Covers dropped webhooks and slow STK confirmations.
- **Nightly reconciliation:** list Paystack transactions for the day, diff against `payment_intent`
  and `ledger`. Alert on any Paystack `success` with no corresponding credit grant, and on any
  balance ≠ SUM(ledger).

---

## 7. Chat surface

Two new orchestrator tools, plus a status the model already knows how to relay:

| Tool | Returns |
|---|---|
| `get_credit_balance(client_id)` | balance, held, available, free-trial state |
| `create_topup_link(amount, currency)` | STK push initiated, or `authorization_url` |

Insufficient balance is **not** an exception — `_dispatch_audit_image_ref` returns a status dict the
model turns into copy, exactly like the existing `under_pressure` pattern:

```json
{
  "status": "insufficient_credits",
  "required_credits": 20,
  "available_credits": 5,
  "instruction": "Tell the user they need 15 more credits for a skin audit and offer a top-up link. Do NOT retry this tool."
}
```

Also: a low-balance nudge appended after a debit that leaves < 1 audit's worth, and a top-up
confirmation message on `charge.success`.

---

## 8. Module layout

```
dermascan/billing/
  __init__.py
  db.py             # psycopg pool, reuses POSTGRES_* env
  accounts.py       # get_or_create_account, free-trial grant, channel linking
  pricing.py        # JOB_CREDIT_COSTS, currency tiers, min top-up
  ledger.py         # append-only writes, balance mutation under FOR UPDATE
  reservations.py   # reserve / commit / release / sweep_expired
  paystack_client.py# initialize, charge (STK), verify
  webhook.py        # SHA512 signature verification
  flags.py          # BILLING_ENABLED, BILLING_ENFORCE, BILLING_CHARGE_CHAT_TURNS
migrations/001_billing.sql
dermascan/backend/services/agents/billing_worker.py   # billing:jobs + reservation sweeper + verify poller
```

Add `billing_worker` to `scripts/do_work.py` alongside orchestrator / escalation / outbound.

### Feature flags (default OFF — matches the cosmetic-pivot convention)

| Variable | Default | Purpose |
|---|---|---|
| `BILLING_ENABLED` | `false` | Master switch. Off = today's behavior exactly |
| `BILLING_ENFORCE` | `false` | **Shadow mode.** Records what *would* be charged without blocking |
| `BILLING_CHARGE_CHAT_TURNS` | `false` | Meter orchestrator turns too |
| `BILLING_FREE_TRIAL_CREDITS` | `40` | One-time grant per account |
| `PAYSTACK_SECRET_KEY` | — | `sk_live_…` / `sk_test_…` |
| `PAYSTACK_PUBLIC_KEY` | — | client-side, if a web checkout page is added |
| `PAYSTACK_WEBHOOK_IP_ALLOWLIST` | `true` | Enforce the three Paystack IPs |
| `BILLING_RESERVATION_TTL_S` | `1800` | Sweeper releases stuck holds |

**Shadow mode is the important one.** Run phase 1 for a week or two, look at what real users would
have been charged, and tune the price list before anything blocks a single request.

---

## 9. Rollout phases

| Phase | Scope | Flags | Exit criteria |
|---|---|---|---|
| 0 | Schema + `dermascan/billing/` module + unit tests | all off | migrations apply, tests green |
| 1 | **Shadow mode** — accounts auto-created, reservations recorded, nothing blocks | `BILLING_ENABLED=true`, `ENFORCE=false` | 1–2 weeks of real data; price list tuned |
| 2 | Top-up live — Paystack test keys → live keys, webhook, verify poller | + real keys | test KES 100 top-up credits correctly end to end |
| 3 | Enforce on `skin_audit` + `product_review_*` | `ENFORCE=true` | insufficient-credit copy reads well in chat |
| 4 | Enforce on `specialist_escalation` | — | derm cost confirmed |
| 5 | Reconciliation job + balance-drift alerting | — | nightly clean for 7 days |

---

## 10. Open decisions

1. ~~**Escalation price.**~~ **Decided 2026-08-13: KES 40** (40 credits). Loss-making if derm payout
   per case exceeds KES 40 — tracked as a deliberate pilot cost, monitored in the admin portal.
2. **Cross-channel identity.** Same human on WhatsApp and Telegram gets two accounts and two free
   trials. Merge via OTP at first top-up, or accept the leakage for pilot?
3. **Email.** Paystack requires one. Collect at first top-up (better — enables receipts), or
   synthesize `{hash}@qutis.app`?
4. **Failed-audit policy.** Auto-release is designed in. Do you also want a goodwill re-run when
   MedGemma returns a low-quality result the user complains about?
5. **Currencies.** Is your Paystack account Kenya-only (KES settlement), or multi-country? That
   determines whether the non-KES tiers in §3 are reachable at all.

---

## Related docs

- Cloud deploy: `dermascan/docs/cloud-deployment-plan.md` — billing worker is another Cloud Run
  service; `PAYSTACK_SECRET_KEY` goes in Secret Manager, and the webhook needs a stable public URL
- Feature-flag convention: `dermascan/docs/cosmetic-pivot.md`
- Environment keys: `.env_example`

# Credit Deduction Contract

Interface between the billing layer (Munene) and the orchestrator's deduction
checkpoints (Kelvin). Everything described here is built, tested, and on
`feat/billing-paystack-credits`.

**Boundary:** billing owns anything touching money or Postgres. The orchestrator
owns *calling* `deduct()` at the three checkpoints and counting tokens.

---

## 1. The model in one paragraph

Postgres owns money; Redis owns the live spendable balance. A trip deducts
several times and settles to Postgres **once**, so checkpoint count stops
driving write volume. There is no reservation table and no "held" pool —
deduction is destructive, so credits taken by one trip are simply gone from the
balance and no concurrent trip can spend them.

```
gate (bridge)     deductions (N per trip)        end of trip
─────────────     ───────────────────────        ───────────
get_balance()     deduct() × N                   settle()
O(1), no DB       one Lua call each              1 Postgres write
```

---

## 2. The API

```python
from dermascan.billing import redis_wallet as wallet
```

### `wallet.deduct(account_id, *, credits, checkpoint, request_id, overdraft_floor=0) -> DeductResult`

Takes credits for one checkpoint. Atomic: the check and the decrement happen
inside one Lua script, so two concurrent trips cannot both spend the same
credit.

`checkpoint` is a free-text meter name — use `llm_call`, `tool_call`,
`medgemma`. It becomes a field in the trip hash and lands in the ledger row's
`metadata.checkpoints`, which is what the admin portal breaks costs down by.

`request_id` groups checkpoints into one trip. Use the same value the job
envelope carries.

`DeductResult` has `.code`, `.balance`, `.ok`, `.is_cold`:

| code | constant | meaning | what to do |
|---|---|---|---|
| `1` | `wallet.DEDUCTED` | credits taken | continue |
| `0` | `wallet.INSUFFICIENT` | balance would fall below the floor | stop, tell the user (see §4) |
| `-1` | `wallet.COLD` | **cache miss, not a zero balance** | `wallet.hydrate(account_id)`, then retry once |

> The cold case is the one that will bite. A missing Redis key means we have not
> loaded the balance yet, not that the user is broke. Treating `-1` as "no
> credits" locks out paying users.

### `wallet.settle(account_id, request_id, *, job_type=None) -> int`

Writes the trip's total to Postgres as one ledger row and clears the Redis trip
state. Returns credits settled. Call it once when the trip finishes — including
on the failure path, since the tokens were really spent.

Safe to call twice: the ledger's `idempotency_key` (`trip:{request_id}`) makes
the second call a no-op returning 0. If it never gets called, the reaper in
`billing_worker` settles the trip after 30 minutes, so nothing is lost — but
the user's balance reads low until then, so call it.

### `wallet.hydrate(account_id) -> int`

Loads `postgres.balance - unsettled` into Redis. Call on `COLD`.

### `wallet.get_balance(account_id, *, hydrate_if_cold=True) -> int`

For the gate before a request enters the orchestrator.

### `wallet.set_pending_task(account_id, description)`

Call when you turn a request away for insufficient credits. After a successful
payment the billing worker reads it and asks the user whether to continue —
`"You had {description} waiting. Want me to continue with it?"` So write
something that reads naturally there: `"a skin review"`, not `"skin_audit"`.

### Resolving an account

```python
from dermascan.billing import accounts
account = accounts.resolve_for_client(channel, client_id)   # grants the free trial on first contact
```

`orchestrator_worker` already calls `accounts.remember_channel(client_id, channel)`
when a job is picked up, so tools that only have a `client_id` can resolve the
channel via `accounts.resolve_channel(client_id)`.

---

## 3. Rates

Token rates live in `billing.price_list` under scope `model_token_rate`, keyed
however you like — `gemini-2.5-flash:input` is the shape I would suggest. Read
them with:

```python
from dermascan.billing import pricing
rate = pricing.get_price(pricing.SCOPE_MODEL_TOKEN_RATE, "gemini-2.5-flash:input")
```

Cached for 60s in-process, so reading per checkpoint is fine. They are editable
from `/admin/pricing` and versioned by `effective_from`, so a past charge stays
explainable by the rate that was in force when it ran. **The table is empty for
model rates** — populate it as part of your work, either via
`pricing.set_price(...)` or a `002_*.sql` migration.

Flat per-deliverable prices are already seeded: `pricing.job_credits("skin_audit")`
returns 20, and so on.

---

## 4. Recommended usage

```python
result = wallet.deduct(
    account.id, credits=cost, checkpoint="llm_call", request_id=request_id,
    overdraft_floor=int(pricing.setting("overdraft_floor")),   # seeded at -60
)

if result.is_cold:
    wallet.hydrate(account.id)
    result = wallet.deduct(...)          # retry once

if not result.ok:
    wallet.set_pending_task(account.id, "a skin review")
    return {
        "status": "insufficient_credits",
        "instruction": "Tell the user they're out of credits and offer create_topup.",
    }
```

Return a status dict rather than raising, matching the existing
`under_pressure` convention — the model turns it into copy.

**On the overdraft floor.** It is negative on purpose. A trip that runs out at
checkpoint 8 of 12 has already cost us the GPU seconds and the provider tokens;
cutting there wastes that *and* hands the user a broken deliverable. Letting the
tail run a few credits negative costs a few shillings and delivers the report.
The gate still hard-stops a *new* trip at zero, so exposure is one trip deep.

---

## 5. Things that will surprise you

- **The client lock does not cover the whole trip.** `_acquire_client_lock` is
  released at `orchestrator_worker.py:1797`, before the MedGemma signal flow
  runs. So a second turn can spend credits while trip A's tail is still pending.
  That is what the overdraft floor absorbs.
- **A top-up landing mid-trip needs no coordination.** `apply_topup` uses
  `INCRBY`, which commutes with concurrent `DECRBY`. Never write an absolute
  balance to Redis — it would clobber in-flight spend.
- **Keys are hash-tagged by account** (`bal:{acct}`, `unsettled:{acct}`,
  `trip:{acct}:{request_id}`) so they land on one Redis Cluster slot. If you add
  keys the deduct script touches, tag them the same way or you get CROSSSLOT.
- **`unsettled` is not a second balance.** It is how much of the decrement has
  not reached Postgres. The gate reads `bal` alone; subtracting `unsettled`
  again would charge the user twice.

---

## 6. What is already running

`billing_worker` (registered in `dermascan/scripts/do_work.py`) runs the stream
consumer, the payment verify poller, and the stale-trip reaper. Nothing extra to
start.

Flags: `BILLING_ENABLED` gates everything and defaults off.
`BILLING_ENFORCE` and `BILLING_CHARGE_CHAT_TURNS` are deliberately **not**
declared yet — they belong to your half, so add them when something reads them.

---

## 7. Open item on your side

Confirm whether LangSmith exposes an API for per-run token and cost data. If it
does, feeding it into `billing.price_list` beats re-deriving rates by hand.

## Related

- `dermascan/docs/billing-credits-plan.md` — the original plan. Its §4
  reserve/commit design is **superseded** by this document; the rest still holds.
- `dermascan/docs/admin-billing-portal-plan.md` — the portal, now built in
  `/Projects/qutis-escalation` under `app/(admin)`.

# Async CIR index worker (Tier 1)

Moves lazy PDF → chunk → embed → Pinecone indexing off orchestrator signal threads
while preserving stream heartbeats, reclaim, and idempotent user messaging.

## Streams and groups

| Name | Default | Role |
|------|---------|------|
| `RULES_INDEX_STREAM` | `rules:index:jobs` | One message per ingredient index job |
| `RULES_INDEX_DLQ_STREAM` | `rules:index:jobs:dlq` | Failed jobs after retries |
| `RULES_INDEX_CONSUMER_GROUP` | `rules-index-workers` | Index worker consumers |

Completion re-enqueues **`orchestrator:signals`** with `signal_type=product_review_index_ready`
(final pass). Interim user copy is sent from the **short** product signal handler when any
ingredient is `source=queued`.

## Redis keys (coordination)

| Key pattern | Purpose |
|-------------|---------|
| `rules:index:queued:{normalized_name}` | Dedupe in-flight index enqueue (SETNX + TTL) |
| `rules:index:batch:{request_id}` | SET of normalized names still pending for one product review |
| `rules:index:context:{request_id}` | JSON snapshot to re-run final rules pass (TTL 7d) |
| `rules:product_report_sent:{request_id}` | Final outbound idempotency |
| `rules:product_index_pending_sent:{request_id}` | Interim outbound idempotency |

## Signal types

- Existing: `product_review_*` (initial completion from diagnose / intake)
- New: `product_review_index_ready` — indexing batch finished (success, partial, or failed)

## User-visible flow

1. Product signal runs ingredient extraction → `ensure_reports_exists` queues missing CIR PDFs.
2. If any queued: send interim message (“examining ingredients and safety; we will notify you”),
   ACK signal quickly.
3. `indexing_worker` consumes jobs (serial default), heartbeats long jobs, marks Redis index cache.
4. When batch SET empty: emit `product_review_index_ready`.
5. Final handler runs CIR rules + report LLM; on index failure or thin evidence, **web fallback**
   via product name + `report_internet_search` so the user always receives a safety report.

## Guardrails (same as other workers)

- `get_redis_client()` + `run_with_redis_retries()` for Redis I/O
- Consumer group + reclaim + **`start_stream_heartbeat`** on index messages (multi-minute PDF work)
- `WORKERS_STREAM_MAXLEN`, DLQ, structured logging
- `RULES_INDEX_INLINE=true` — dev-only synchronous index (legacy)

## Rollout flags

- `RULES_INDEX_INLINE` — default `true` until orchestrator defer/final pass is shipped; set `false` with worker enabled
- `RULES_INDEX_WORKER_ENABLED` — starts `indexing_worker` thread in `do_work.py`

## Index worker (Step 3)

- Module: `indexing_worker.py`
- `INDEX_WORKER_MAX_INFLIGHT=1` (serial PDF/embed load)
- Stream heartbeat during each job (reclaim-safe)
- On success: Redis index cache + batch `SREM` + optional `product_review_index_ready`
- On failure: DLQ + batch failure set + still advances batch (final pass uses partial/fallback later)

**Note:** On dedupe skip, if the ingredient is already in Redis cache (index finished elsewhere), the
batch is not incremented and the product path may proceed without defer.

## Product signal defer (Step 5)

When `RULES_INDEX_INLINE=false` and any ingredient is `source=queued`:

1. Save `rules:index:context:{request_id}` for the final pass (Step 7).
2. Send interim outbound once (`rules:product_index_pending_sent:{request_id}`).
3. Skip full `assess_product_review` / report LLM on the hot path (fast ACK).

Configure copy via `PRODUCT_INDEX_PENDING_MESSAGE` (`{product_name}` placeholder).

## Final pass + fallback (Steps 7–8)

- Signal `product_review_index_ready` → `run_product_signal_rules_flow_index_ready`.
- Loads `rules:index:context:{request_id}`, runs `assess_product_review(index_outcome=…)`.
- Idempotency: `rules:product_report_sent:{request_id}`.
- Internet fallback (Tavily via `internet_search`) when index outcome is partial/failed,
  no indexed IDs, or CIR vector sections are empty — report LLM still renders user copy.

See `.env_example` for tunables.

## Operations (Step 9)

### Observability

- Structured logs: `duration_s` on index job complete/fail; `internet fallback` on final pass.
- **`events:stream`** entries: `rules_index_job_started|completed|failed`, `rules_index_batch_ready_signal` (agent=`rules_index`).

### Batch timeout

- Context stores `enqueued_at_ms` when deferring.
- If final pass runs after **`RULES_INDEX_BATCH_TIMEOUT_SEC`**, a warning is logged.
- **`reconcile_orphan_index_batch`** clears stale `rules:index:batch:{request_id}` members (records failures) before the final report.

### DLQ replay (`rules:index:jobs:dlq`)

1. Inspect: `XREVRANGE rules:index:jobs:dlq + - COUNT 10`
2. For each entry, read `fields` JSON and fix root cause (Firebase PDF, OpenAI embed, Pinecone).
3. Re-enqueue manually: rebuild payload → `enqueue_rules_index_job` (or `XADD rules:index:jobs` with same `payload` field shape as production).
4. Clear dedupe if needed: `DEL rules:index:queued:{normalized_name}`
5. Do **not** replay DLQ by moving messages back blindly if the ingredient is already in `rules_engine:indexed:{normalized}` — check `HGETALL` first.

### Health checklist

| Symptom | Check |
|---------|--------|
| Stuck on interim message, no final report | `rules:index:batch:{request_id}` SCARD; index worker running; `XPENDING rules:index:jobs rules-index-workers` |
| Duplicate interim/final WhatsApp | `GET rules:product_index_pending_sent:{id}` / `rules:product_report_sent:{id}` |
| Redis timeouts during index | `INDEX_WORKER_MAX_INFLIGHT=1`; local Redis for dev; pre-index hot ingredients |
| Empty CIR report only | Tavily key set; look for `evidence_mode=internet_fallback` in logs |

### Production rollout

1. Pre-index common CIR ingredients (optional).
2. `RULES_INDEX_WORKER_ENABLED=true` → restart `do_work.py`.
3. `RULES_INDEX_INLINE=false` on orchestrator-facing env.
4. Confirm one index worker process (avoid duplicate consumers).

## End-to-end validation (Step 10)

### Automated (CI / local)

```powershell
python -m pytest dermascan/tests/unit/test_rules_index_async_flow.py -q
python -m pytest dermascan/tests/unit/test_product_index_defer.py dermascan/tests/unit/test_rules_index_jobs.py dermascan/tests/unit/test_indexing_worker.py -q
```

`test_rules_index_async_flow.py` exercises **enqueue → batch SET → context snapshot → finalize → `product_review_index_ready`** on fakeredis (no PDF/torch).

### Manual — Turn sandbox (WhatsApp)

**Prerequisites**

| Service | Command / check |
|---------|-----------------|
| Redis | Local or Cloud; `REDIS_URL` set |
| Nova API | `uvicorn dermascan.backend.app.main:app` on `:8088` (sandbox env if using Turn relay) |
| Workers | **One** `poetry run python dermascan/scripts/do_work.py` with async flags below |
| ngrok | Points to Nova if Turn webhooks hit your machine |

**Required `.env` for async path**

```env
RULES_INDEX_INLINE=false
RULES_INDEX_WORKER_ENABLED=true
INDEX_WORKER_MAX_INFLIGHT=1
TAVILY_API_KEY_QUTIS=...   # web fallback on index failure
```

**Checklist**

1. **Unindexed ingredient** — Product label with a CIR ingredient not in `rules_engine:indexed:{normalized}` → within ~10s user gets **one interim** message (`PRODUCT_INDEX_PENDING_MESSAGE`); orchestrator signal handler returns quickly (no multi-minute block on signal thread).
2. **Final CIR report** — After index worker logs `index job completed` / `index ready signal enqueued`, user receives **one final** safety report (CIR vector path when index succeeded).
3. **Web fallback** — Force failure (bad `pdf_url` in DB row or DLQ one job) → user still gets **one final** report; logs show `internet fallback` / `evidence_mode=internet_fallback`.
4. **Idempotency** — Replay same Turn webhook or reclaim duplicate `product_review_*` / `product_review_index_ready` → **one** interim and **one** final (`rules:product_index_pending_sent:{request_id}`, `rules:product_report_sent:{request_id}`).
5. **Signal thread health** — While index runs (minutes), confirm **no** sustained `heartbeat missed` / reclaim storms on **orchestrator:signals** for the original product signal (index work must stay on `rules:index:jobs`).

**Redis spot-checks during a stuck run**

```text
SCARD rules:index:batch:{request_id}
XPENDING rules:index:jobs rules-index-workers
GET rules:index:context:{request_id}
XREVRANGE orchestrator:signals + - COUNT 5
```

**Pass criteria:** Interim + final messages match checklist; no duplicate outbound; signal handler latency stays seconds on defer path; DLQ empty or understood after test.

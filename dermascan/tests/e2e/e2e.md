# End-to-End (E2E) Testing Guide

> **Purpose:** Prove the **live Dermascan stack** — backend API, Redis, workers, and LLM
> providers — delivers real user jobs from webhook ingress through orchestrator replies.
>
> **Companion docs:** [`tests.md`](../tests.md) (full pre-deployment plan),
> [`redis_tests.md`](../redis_tests.md) (Redis layer in isolation),
> [`../API/test_ingress_p95.py`](../API/test_ingress_p95.py) (admission-only latency).

---

## 1. Why E2E testing matters

Unit and integration tests (including `redis_tests.md`) prove **components** behave correctly
in isolation. E2E tests prove **wiring** — the production path users actually hit:

```
POST /v1/webhooks/whatsapp
    → admission (rate limit + queue depth)
    → Redis stream orchestrator:jobs
    → orchestrator_worker (Qwen / fallbacks)
    → outbound:messages
    → (optional) outbound_sender → channel
```

| If E2E fails | Likely cause |
| --- | --- |
| Ingress 200 but no outbound reply | Workers not running, LLM down, stream misconfig |
| Timeouts at concurrency 40 | Qwen/Modal saturation, queue backlog, timeout too low |
| 429 storms | RPM or `MAX_QUEUE_LEN_HARD` correctly blocking — tune or scale workers |
| Duplicate webhook crashes | Idempotency / reclaim gap (see smoke duplicate test) |

E2E is the last line before production: **no mocks**, real `.env`, real workers.

---

## 2. What you must run locally

| # | Process | Command | Port |
| --- | --- | --- | --- |
| 1 | Redis | (your Redis service) | `REDIS_URL` in `.env` |
| 2 | Backend API | `poetry run uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port 8088` | **8088** |
| 3 | Workers | `poetry run python dermascan/scripts/do_work.py` | — |
| 4 | Qwen (primary orchestrator LLM) | Deploy + set `QWEN_BASE_URL` in `.env` | Modal |
| 5 | MedGemma | Only when image E2E is re-enabled | **8080** |

**Not MedGemma on 8080** for text E2E — that is inference gateway; ingress lives on **8088**.

Pytest loads repo-root `.env` automatically (`e2e/conftest.py`).

---

## 3. Test pyramid (E2E layer)

```
┌─────────────────────────────────────────────┐
│  Load / ingress p95 (API/test_ingress_p95)  │  admission latency @ c=20,40
├─────────────────────────────────────────────┤
│  Orchestrator success @ 20, 40 parallel     │  full reply validation
├─────────────────────────────────────────────┤
│  Latency (sequential) + concurrency (5)     │  baselines
├─────────────────────────────────────────────┤
│  Smoke (single job + duplicate webhook)     │  fast sanity
└─────────────────────────────────────────────┘
         ▲
         │  builds on redis_tests.md (45+ tests)
```

Marker: `@pytest.mark.e2e` — requires live stack; tests **skip** if API/Redis unreachable.

---

## 4. Suites — what is tested and why

### 4.1 Smoke — `test_pipeline_smoke.py`

| Test | What it asserts | Why it matters |
| --- | --- | --- |
| `test_text_job_completes` | One text webhook → non-empty outbound reply | Minimum viable pipeline |
| `test_duplicate_webhook_replay_completes_without_5xx` | Same `message_id` twice → both complete | Replay safety (ingress dedup still TODO) |
| `test_image_job_completes` | *(skipped)* | Image path disabled for now |

**Success signal:** `wait_for_orchestrator_reply()` finds `text` on `outbound:messages`.

---

### 4.2 Latency — `test_pipeline_latency.py`

| Test | What it asserts | Why it matters |
| --- | --- | --- |
| `test_text_pipeline_latency_percentiles` | 3 sequential jobs → p50/p95/p99 | Baseline turn time without parallel contention |

Measures **webhook → orchestrator reply**, not HTTP enqueue time.

---

### 4.3 Orchestrator success — `test_orchestrator_success.py`

| Test | What it asserts | Why it matters |
| --- | --- | --- |
| `test_parallel_text_jobs_return_outbound_replies[20]` | 20 parallel text jobs, all get replies | Production-like text burst |
| `test_parallel_text_jobs_return_outbound_replies[40]` | 40 parallel text jobs, all get replies | Upper bound before pre-production sign-off |

**Pass criteria:** `success_count == job_count` and every reply has non-empty `text`.

Example observed baselines (your stack, text-only):

| Concurrency | Success | p95 (orchestrator reply) |
| --- | --- | --- |
| 20 | 20/20 | ~11s |
| 40 | 40/40 | ~10.6s |

LLM latency dominates; Redis is rarely the bottleneck here.

---

### 4.4 Concurrency (light) — `test_pipeline_concurrency.py`

| Test | What it asserts | Why it matters |
| --- | --- | --- |
| `test_parallel_text_jobs_complete` | 5 parallel jobs (default), all replies | Quick parallel sanity between smoke and 20/40 |

---

### 4.5 Admission latency — `../API/test_ingress_p95.py` (related, not under `e2e/`)

| Test | What it asserts | Why it matters |
| --- | --- | --- |
| `test_ingress_p95_whatsapp` | Enqueue p95 at concurrency **20 and 40** | Proves admission gates under burst; separate from orchestrator reply time |

**Important:** High admission p95 (e.g. 10–17s) under c=20 means requests **waited** in
`_wait_for_queue_capacity`, not that orchestrator took that long. All-200 still means
nothing was silently dropped.

Results → `dermascan/tests/API/ingress_p95_results.csv`

---

## 5. Results CSV (required)

Every passing E2E test **appends one row** to:

```
dermascan/tests/e2e/e2e_results.csv
```

Override path:

```powershell
$env:DERMASCAN_E2E_RESULTS_CSV = "C:\path\to\my_e2e_results.csv"
```

### 5.1 Columns

| Column | Description |
| --- | --- |
| `timestamp` | UTC ISO-8601 when the row was written |
| `suite` | `smoke`, `latency`, `orchestrator`, `concurrency` |
| `test_name` | Pytest test identifier |
| `status` | `passed` (on successful assertion path) |
| `concurrency` | Parallel workers for the run |
| `job_count` | Total jobs attempted |
| `success_count` | Jobs that completed successfully |
| `success_rate` | `success_count / job_count` |
| `p50_ms`, `p95_ms`, `p99_ms` | Orchestrator reply latency percentiles |
| `min_ms`, `max_ms`, `avg_ms` | Latency spread |
| `sample_reply` | First 200 chars of orchestrator outbound text |
| `notes` | Free-form (round count, dedup notes, etc.) |

Rows are **appended** across runs so you can track regressions over time.

Implementation: `record_e2e_result()` in [`stack_helpers.py`](stack_helpers.py).

---

## 6. Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `REDIS_URL` | *(required)* | Same Redis as backend/workers |
| `WHATSAPP_INGRESS_SECRET` | or `INTERNAL_API_SECRET` | Webhook auth |
| `DERMASCAN_API_URL` | `http://localhost:8088` | Backend base URL |
| `DERMASCAN_E2E_PIPELINE_TIMEOUT_S` | `180` | Max wait per job |
| `DERMASCAN_E2E_CONCURRENCY_LEVELS` | `20,40` | Orchestrator success parametrize |
| `DERMASCAN_E2E_PARALLEL_JOBS` | `5` | Light concurrency test |
| `DERMASCAN_E2E_PIPELINE_ROUNDS` | `3` | Sequential latency sample count |
| `DERMASCAN_E2E_RESULTS_CSV` | `e2e/e2e_results.csv` | Results output path |
| `DERMASCAN_P95_CONCURRENCY_LEVELS` | `20,40` | Ingress p95 sweep (API tests) |

---

## 7. How to run

### Full E2E suite (text-only, active tests)

```powershell
poetry run pytest dermascan/tests/e2e -m e2e -v -s
```

### Recommended order

```powershell
# 1) Smoke (~45s)
poetry run pytest dermascan/tests/e2e/test_pipeline_smoke.py -m e2e -v -s

# 2) Latency baselines
poetry run pytest dermascan/tests/e2e/test_pipeline_latency.py -m e2e -v -s

# 3) Orchestrator @ 20 + 40 (heavy — allow 300s timeout)
$env:DERMASCAN_E2E_PIPELINE_TIMEOUT_S = "300"
poetry run pytest dermascan/tests/e2e/test_orchestrator_success.py -m e2e -v -s

# 4) Admission p95 @ 20 + 40
poetry run pytest dermascan/tests/API/test_ingress_p95.py::test_ingress_p95_whatsapp -m load -v -s
```

After each run, inspect:

- `dermascan/tests/e2e/e2e_results.csv`
- `dermascan/tests/API/ingress_p95_results.csv`

---

## 8. Pass / fail criteria

| Criterion | Evidence |
| --- | --- |
| **Pipeline completes** | Smoke text test passes; row in `e2e_results.csv` |
| **Orchestrator replies** | Non-empty `sample_reply`; `success_rate = 1.0` |
| **Parallel text @ 40** | `orchestrator` suite row: `success_count=40` |
| **No server errors on ingress** | Ingress p95: no 5xx; only 200 and optional 429 |
| **Results persisted** | CSV appended every passing test (see console: `E2E CSV appended: ...`) |
| **Duplicate webhook safe** | Smoke duplicate test: 2/2 complete, no 5xx |

---

## 9. Directory layout

```
dermascan/tests/e2e/
├── e2e.md                          ← this guide
├── e2e_results.csv                 ← appended results (created on first run)
├── conftest.py                     ← loads .env, stack fixtures
├── stack_helpers.py                ← webhook helpers, wait/poll, CSV writer
├── test_pipeline_smoke.py
├── test_pipeline_latency.py
├── test_orchestrator_success.py
└── test_pipeline_concurrency.py
```

---

## 10. Image E2E (currently disabled)

Image tests are `@pytest.mark.skip` until you add a supported sample under
`dermascan/tests/images/` (`.jpg`, `.png`, or `.webp` — not AVIF) and re-enable
MedGemma. When enabled, they validate the diagnosis tool path end-to-end.

---

## 11. What E2E does *not* cover yet

- Ingress-level webhook dedup (R10) — duplicate test documents current behavior
- Sustained load / soak (Locust — see `load/profiles.md`)
- Real WhatsApp/Telegram channel delivery (outbound stream is sufficient for reliability)
- Image / MedGemma pipeline (skipped)
- Provider failover chaos (Qwen down → OpenAI/Gemini)

See `redis_tests.md` for transport-layer reliability already covered in CI.

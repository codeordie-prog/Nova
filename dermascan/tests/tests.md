# Dermascan — Pre-Deployment Test & Stress-Testing Plan

> Scope: reliability, scalability, concurrency, and limits for the Dermascan
> agents network before production deployment.
>
> This document is the **planning spec** for the test suite. It enumerates the
> concerns to cover, the recommended tooling, and the scaffold/structure for the
> tests. Implementation follows this layout.

---

## 1. System Under Test (SUT) — what we are actually stressing

The platform is an async, Redis-Streams-backed multi-agent pipeline. The
stress-relevant surfaces are:

- **Ingress (FastAPI)** — `dermascan/backend/api/endpoints.py`
  - `POST /v1/webhooks/whatsapp`, `POST /v1/webhooks/telegram`
  - `POST /v1/orchestrator/requests`, `/batch`, `GET /.../{request_id}`
  - `POST /v1/media/test/upload`, `GET /health`
- **Admission control / backpressure**
  - Global rate limiter — `dermascan/shared/global_rate_limiter.py` (Redis fixed-window, RPM)
  - Queue depth manager — `dermascan/shared/queue_depth_manager.py` (green/yellow/red on `XPENDING`)
- **Redis Streams transport** — `orchestrator:jobs` + per-agent job/DLQ streams, consumer groups
- **Workers (consumers)** — `orchestrator_worker.py`, `knowledge_worker.py`,
  `outbound_sender_worker.py`, `diagnose`, `escalation`, `validation`, MedGemma modal worker
- **Resilience primitives**
  - Per-client distributed lock — `dermascan/backend/services/agents/locks.py`
  - Pending-message recovery (XAUTOCLAIM/DLQ/idempotency) — `reclaim_logic.py`
  - Provider circuit breaker — `provider_circuit_breaker.py`
  - Redis retry/backoff client — `dermascan/shared/redis_stability.py`
- **Rules engine + vector store** — `dermascan/rules_engine/` (Pinecone-backed)
- **External dependencies** — MedGemma (Modal), Qwen, Google CSE / Tavily, Firebase storage, Postgres

**Key system invariant under load** (from `global_rate_limiter.py` docstring):
> input rate < drain rate of the Redis stream — to avoid backpressure and prevent
> overwhelming the inference layer.

Most of our scalability assertions exist to prove this invariant holds.

---

## 2. Recommended tooling

| Layer | Tool | Why |
| --- | --- | --- |
| Test runner | **pytest** (already a dep, `^9.0.3`) | standard, fixtures, markers |
| Async tests | **pytest-asyncio** | the ingress + admission gates are `async` |
| Redis isolation | **fakeredis[lua]** (unit) + **real Redis via testcontainers/docker** (integration) | Lua-script lock + streams need real semantics for integration |
| HTTP unit | **fastapi.TestClient** / **httpx.AsyncClient** | in-process endpoint tests, no live server |
| Concurrency probes | **concurrent.futures.ThreadPoolExecutor** (already used) | matches existing `tests/API/` style |
| Load / soak | **Locust** (Python, scriptable) or **k6** (JS) | sustained RPS, ramp profiles, p95/p99 |
| Property/fuzz | **hypothesis** | malformed payloads, fuzzing image bytes + schemas |
| Time control | **freezegun** | rate-limit window rollover without sleeping |
| Coverage | **pytest-cov** | gate critical modules |
| Fault injection | **pytest monkeypatch** + **toxiproxy** (optional) | simulate Redis latency/drops, provider outages |

**Recommendation:** pytest for unit + integration assertions; **Locust** for the
sustained load/soak profiles (richer reporting than the current ad-hoc CSV
ThreadPool probes, which we keep as quick smoke probes).

Add these to a `[tool.poetry.group.test.dependencies]` group:
`pytest-asyncio`, `pytest-cov`, `fakeredis`, `httpx`, `hypothesis`, `freezegun`,
`locust`, `testcontainers`.

---

## 3. Test taxonomy (markers)

Register markers in `pyproject.toml`/`pytest.ini` so suites run independently:

```
unit          # pure logic, mocked Redis/providers, <50ms each, run in CI on every push
integration   # real Redis (container), real streams/locks, no external paid APIs
concurrency   # multi-thread/process correctness + race conditions
load          # sustained RPS, Locust/k6, run on-demand / nightly, needs a live stack
soak          # long-duration stability (memory/leaks/drift)
chaos         # fault injection (Redis drop, provider down, worker kill)
slow          # anything > a few seconds
```

Run examples:
- `pytest -m "unit"` (CI gate)
- `pytest -m "integration and not slow"`
- `pytest -m concurrency`
- `locust -f tests/load/locustfile.py`

---

## 4. Proposed directory scaffold

Builds on the existing `dermascan/tests/` (keep `tests/API/` probes; add structure):

```
dermascan/tests/
├── tests.md                      # this plan
├── conftest.py                   # shared fixtures (redis, app client, sample images, env)
├── pytest.ini                    # markers, asyncio mode, addopts
├── fixtures/
│   ├── payloads.py               # valid/invalid ingress envelopes (whatsapp/telegram/orchestrator)
│   ├── images.py                 # jpeg/png/webp/gif headers, oversized, corrupt
│   └── redis_factory.py          # fakeredis + real-redis (testcontainers) fixtures
│
├── unit/
│   ├── test_rate_limiter.py      # fixed-window counting, window rollover, RPM=0 bypass
│   ├── test_queue_depth.py       # green/yellow/red thresholds, wait budget exhaustion
│   ├── test_circuit_breaker.py   # closed→open→half_open→closed transitions, threading
│   ├── test_locks.py             # NX PX acquire, compare-and-delete release, TTL expiry
│   ├── test_reclaim_logic.py     # XAUTOCLAIM parse, idempotency, max attempts→DLQ
│   ├── test_redis_stability.py   # retry/backoff, jitter bounds, exhaustion re-raise
│   ├── test_image_validation.py  # magic-byte sniffing, size cap (5MB), empty/garbage
│   └── test_request_models.py    # pydantic schema validation / boundary fields
│
├── integration/                  # real Redis container required
│   ├── test_ingress_enqueue.py   # webhook → stream entry shape, ack response
│   ├── test_admission_gates.py   # rate-limit 429 + queue red 429 end-to-end at ingress
│   ├── test_stream_consumer_group.py  # XADD/XREADGROUP/XACK lifecycle, MAXLEN trim
│   ├── test_lock_serialization.py     # concurrent holders serialize on client_id
│   ├── test_reclaim_recovery.py       # crashed-consumer pending → reclaimed/DLQ
│   └── test_idempotency.py            # duplicate msg_id processed once
│
├── concurrency/
│   ├── test_lock_race.py         # N threads contend, exactly one critical section at a time
│   ├── test_rate_limiter_race.py # N parallel consumes never exceed RPM
│   ├── test_queue_depth_race.py  # depth reads stable under concurrent enqueue/ack
│   └── test_dedup_under_race.py  # media-batch + idempotency under burst
│
├── load/                         # Locust/k6 — needs live FastAPI + Redis + workers
│   ├── locustfile.py             # ramp profiles, p50/p95/p99, RPS, error rate
│   ├── profiles.md               # documented ramp/spike/soak scenarios + pass criteria
│   └── scenarios/
│       ├── ingress_only.py       # admission-control limits (no inference)
│       ├── full_pipeline.py      # ingress→worker→outbound, mock inference layer
│       └── mixed_media.py        # text + image + media-group bursts
│
├── chaos/
│   ├── test_redis_outage.py      # Redis drops mid-flight → retries / graceful 5xx
│   ├── test_provider_outage.py   # Qwen/MedGemma down → circuit opens, failover
│   └── test_worker_kill.py       # kill consumer mid-job → reclaim re-delivers
│
└── API/                          # existing quick ThreadPool stress probes (keep)
    ├── test_diagnose_concurrency.py
    ├── test_diagnose_p95.py
    └── test_diagnose_stress.py
```

---

## 5. Major concerns to test — by goal

### 5.1 RELIABILITY (correctness under failure)

| # | Concern | Where | Test idea | Pass criteria |
| --- | --- | --- | --- | --- |
| R1 | **Exactly-once-ish processing** | `reclaim_logic.py` | enqueue 1 job, force reclaim twice via idempotency marker | handler runs once; second pass acks+deletes |
| R2 | **Crashed worker recovery** | `reclaim_logic.reclaim_stale_pending` | deliver to consumer A, never ack, advance idle time, reclaim to B | message re-delivered and processed by B |
| R3 | **Poison-message → DLQ** | reclaim max attempts | exceed `WORKERS_MAX_RECLAIM_ATTEMPTS` | message moved to DLQ with reason metadata, acked from source |
| R4 | **Redis transient failure** | `redis_stability.run_with_redis_retries` | raise `ConnectionError` N-1 times then succeed | succeeds; backoff respected; logs warning |
| R5 | **Redis hard failure** | same | raise on all attempts | original error re-raised after `retry_attempts` |
| R6 | **Lock safety on TTL expiry** | `locks.py` Lua release | holder A's TTL expires, B acquires, A releases | A's release is a no-op (compare-and-delete) |
| R7 | **Provider failover** | `provider_circuit_breaker.py` | 3 consecutive Qwen failures | circuit opens, `allow()` False during cooldown, half-open probe after |
| R8 | **Fail-open persistence bridge** | `product_rules_signal_helper.py` | Redis error on skin-profile load/store | flow continues, warning logged, no crash |
| R9 | **Malformed/forged ingress** | `endpoints.py` secret verify + validators | bad `X-Internal-Secret`, missing fields | 401/422; never enqueued |
| R10 | **Idempotent webhook re-delivery** | ingress + dedup | same provider message delivered twice | single job enqueued |
| R11 | **Stream trimming doesn't drop in-flight** | `WORKERS_STREAM_MAXLEN` | flood beyond MAXLEN | pending/unacked not silently lost (approx-trim aware) |

### 5.2 SCALABILITY (does it hold the invariant as volume grows)

| # | Concern | Where | Test idea | Pass criteria |
| --- | --- | --- | --- | --- |
| S1 | **Admission caps input to drain rate** | rate limiter + queue depth | sustained RPS > RPM | excess returns 429 w/ `Retry-After`; queue never unbounded |
| S2 | **Rate-limit window accuracy** | `_consume_global_window` | exactly RPM allowed per window, +1 rejected | count boundary correct; rollover resets (freezegun) |
| S3 | **Queue backpressure thresholds** | `_check_queue_pressure` | depth at soft/hard boundaries | green<soft, yellow in-between, red>=hard |
| S4 | **Wait-budget behavior** | `_wait_for_queue_capacity` | yellow then green within budget; red immediate | yellow waits+retries, red returns instantly |
| S5 | **Throughput vs. concurrency curve** | full pipeline (Locust) | ramp 1→N concurrent | throughput plateaus, latency degrades gracefully (no cliff/crash) |
| S6 | **Horizontal worker scaling** | consumer group | 1 vs 4 workers on same stream | drain rate scales ~linearly; no double-processing |
| S7 | **Connection-pool ceiling** | `redis_stability` max_connections=200 | concurrency > pool | no pool-exhaustion deadlock; bounded waits |
| S8 | **Memory stability under soak** | workers | 30–60 min steady load | RSS flat; no unbounded growth in streams/keys |

### 5.3 CONCURRENCY (race correctness)

| # | Concern | Where | Test idea | Pass criteria |
| --- | --- | --- | --- | --- |
| C1 | **Per-client serialization** | `client_lock` | N threads same `client_id` | critical sections never overlap; checkpoint not corrupted |
| C2 | **Different clients parallel** | `client_lock` | N threads distinct ids | run concurrently (no false serialization) |
| C3 | **Rate-limiter atomicity** | `INCRBY` pipeline | M threads hammer one window | total allowed == RPM exactly, no over-admission |
| C4 | **Single delivery per group** | consumer group | 2 consumers, 1 message | exactly one consumer gets it |
| C5 | **Circuit-breaker thread safety** | breaker `_lock` | concurrent record_failure/success | state transitions consistent, no torn reads |
| C6 | **Media-group batching race** | `group_media_helper.py` | burst album members | one batch claimed, quiet-window respected, no dup enqueue |
| C7 | **Idempotency under burst** | `mark_processed` SET NX | parallel reclaim of same msg | only one wins the marker |

### 5.4 LIMITS (find the breaking points — measure, don't assume)

| # | Limit to discover | Method | Output |
| --- | --- | --- | --- |
| L1 | **Max sustainable RPS at ingress** | Locust ramp until error-rate > 1% | documented RPS ceiling |
| L2 | **Concurrency level where 429s begin** | existing `test_diagnose_concurrency` style | first-throttle concurrency (CSV) |
| L3 | **p95/p99 latency vs. load** | `test_diagnose_p95` + Locust | latency curve + SLO line |
| L4 | **Queue depth ceiling before red** | drive `XPENDING` up | confirm `MAX_QUEUE_LEN_HARD` enforced |
| L5 | **Max image size / payload** | boundary at 5 MB cap | 413 above cap, accept at/below |
| L6 | **Redis connection ceiling** | concurrency beyond pool=200 | behavior documented (wait vs error) |
| L7 | **Stream length / MAXLEN trim point** | flood stream | trim behavior + retention window |
| L8 | **Provider timeout / cold-start ceiling** | MedGemma cold start under load | timeout handling, circuit response |
| L9 | **Lock wait-budget timeout** | hold lock > `CLIENT_LOCK_WAIT_MS` | `LockAcquisitionError` raised cleanly |

---

## 6. Test scaffold conventions

### 6.1 `conftest.py` — core fixtures

```python
import pytest

@pytest.fixture
def fake_redis():
    """In-memory Redis (fakeredis) with Lua support for unit tests."""
    import fakeredis
    return fakeredis.FakeStrictRedis(decode_responses=True)

@pytest.fixture(scope="session")
def real_redis():
    """Real Redis via testcontainers for integration/concurrency suites."""
    from testcontainers.redis import RedisContainer
    with RedisContainer("redis:7") as rc:
        yield rc.get_client(decode_responses=True)

@pytest.fixture
def app_client(monkeypatch, fake_redis):
    """FastAPI TestClient with Redis + external deps patched."""
    from fastapi.testclient import TestClient
    from dermascan.backend.app.main import app
    # monkeypatch endpoints._redis / admission gates as needed
    return TestClient(app)

@pytest.fixture
def sample_images():
    """Valid jpeg/png/webp + corrupt + oversized byte payloads."""
    ...
```

### 6.2 Unit test pattern (admission control example)

```python
import pytest

@pytest.mark.unit
class TestGlobalRateLimiter:
    def test_allows_up_to_rpm(self, monkeypatch, fake_redis):
        # patch _get_redis -> fake_redis; set _RPM=5
        # consume 5 -> allowed; 6th -> not allowed, retry_after >= 1
        ...

    def test_window_rollover_resets(self, monkeypatch, fake_redis, freezer):
        # exhaust window, advance time past _WINDOW_S, consume -> allowed again
        ...

    def test_rpm_zero_bypasses(self, monkeypatch):
        # _RPM <= 0 -> _enforce_global_rate_limit returns without touching redis
        ...
```

### 6.3 Concurrency test pattern (lock race)

```python
@pytest.mark.concurrency
def test_client_lock_serializes(real_redis):
    from concurrent.futures import ThreadPoolExecutor
    counter = {"in_section": 0, "max_overlap": 0}
    def critical():
        from dermascan.backend.services.agents.locks import client_lock
        with client_lock("client-1", redis_client=real_redis, wait_ms=10000):
            counter["in_section"] += 1
            counter["max_overlap"] = max(counter["max_overlap"], counter["in_section"])
            ...  # tiny sleep
            counter["in_section"] -= 1
    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(lambda _: critical(), range(20)))
    assert counter["max_overlap"] == 1   # never overlapped
```

### 6.4 Load profile (Locust skeleton)

```python
# tests/load/locustfile.py
from locust import HttpUser, task, between

class IngressUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def whatsapp_webhook(self):
        self.client.post("/v1/webhooks/whatsapp",
                         headers={"X-Internal-Secret": "<secret>"},
                         json={...valid envelope...})
# Run: locust -f tests/load/locustfile.py --host http://localhost:8080
# Profiles in profiles.md: ramp (1→500 RPS), spike, 60-min soak.
```

---

## 7. Pass/Fail criteria & SLOs (fill in real targets before release)

Define and assert these (start with placeholders, tune from baseline runs):

- **Availability:** success rate >= **99%** under target load (excluding intentional 429s).
- **Admission correctness:** over-RPM requests rejected with **429 + Retry-After**; **0** over-admission.
- **Latency SLO:** ingress (enqueue) p95 < **300 ms**; end-to-end p95 < target (TBD with inference).
- **No data loss:** every accepted job is processed, DLQ'd, or explicitly rejected — **0 silent drops**.
- **No double-processing:** idempotency holds under reclaim + burst.
- **Graceful degradation:** at red/overload → 429s, **never** 5xx storms or worker crashes.
- **Recovery:** after a killed worker / Redis blip, backlog drains to zero within **N** seconds.

---

## 8. Suggested implementation order

1. **Unit** (R4–R7, S2–S4, image/model validation) — fast, no infra, CI gate first.
2. **Integration** with real Redis (R1–R3, R6, R10, ingress enqueue, admission 429).
3. **Concurrency** (C1–C7) — the highest-risk correctness area.
4. **Load** (Locust) to discover limits L1–L4, L6–L7 and build the baseline curve.
5. **Chaos** (R2, R5, R7) — fault injection once happy-path is green.
6. **Soak** (S8) — nightly, long-duration.

CI gate = `unit` + `integration (not slow)` + `concurrency`. `load`/`soak`/`chaos`
run on-demand/nightly against a deployed stack.

---

## 9. Open questions to confirm before writing tests

- Production SLO targets (success rate, p95/p99) — what are the actual numbers?
- Is `/diagnose` (targeted by existing `tests/API/` probes) served by the MedGemma/Modal service rather than the FastAPI gateway in `app/main.py`? Confirm the live base URL/topology for load tests.
- Which external deps can be hit in integration (Redis: yes via container) vs. must be mocked (MedGemma, Qwen, Google CSE, Firebase, Postgres)?
- Target deployment topology (worker replica count, Redis sizing) so scalability numbers are meaningful.

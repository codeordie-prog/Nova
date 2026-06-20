# Redis Reliability Testing Guide

> **Purpose:** Ensure Redis — the backbone of Dermascan's job queue, admission control,
> and distributed coordination — behaves predictably under normal use, transient failures,
> and production load.
>
> **Companion docs:** [`tests.md`](tests.md) (full pre-deployment plan),
> [`dermascan/shared/redis_stability.py`](../shared/redis_stability.py) (shared client + retry layer).

---

## 1. Why Redis reliability matters here

Dermascan is an async, multi-worker pipeline. Redis is not just a cache — it is the
**transport and coordination layer**:

| Redis feature | Used for | If it fails |
| --- | --- | --- |
| **Streams** (`XADD`, `XREADGROUP`, `XACK`) | Job queue between ingress and workers | Jobs stall or are lost |
| **Consumer groups** | Horizontal worker scaling | Double delivery or no delivery |
| **Pending + XAUTOCLAIM** | Crash recovery | Stuck jobs after worker death |
| **Strings + INCRBY** | Global rate limiter (RPM) | Over-admission or false 429s |
| **XPENDING counts** | Queue depth / backpressure | Unbounded backlog |
| **SET NX PX + Lua** | Per-client distributed locks | Corrupted checkpoints |
| **PING / connection pool** | Worker startup health checks | Silent startup on dead Redis |

Production incidents usually look like: brief network blips, connection pool exhaustion,
or Redis restarts — not permanent outages. Our tests prove the system **retries transient
errors**, **fails loudly on hard outages**, and **keeps stream/lock semantics correct**
under concurrency.

---

## 2. The stability layer — what we centralize

All services should use `dermascan/shared/redis_stability.py` instead of ad-hoc
`redis.Redis.from_url(...)` calls. That module provides:

| Function / type | Role |
| --- | --- |
| `RedisStabilityConfig` | Timeouts, pool size, retry counts (env-driven) |
| `load_redis_stability_config()` | Parse + validate env vars |
| `create_redis_client()` | Build a pooled client with keepalive + health checks |
| `get_redis_client()` | Process-wide cached client (one per `decode_responses` mode) |
| `run_with_redis_retries()` | Retry `ConnectionError`, `TimeoutError`, `OSError` with exponential backoff + jitter |
| `check_redis_connectivity()` | Startup `PING` wrapped in retries |

**Consumers today:** `orchestrator_worker`, `outbound_sender_worker`, `global_rate_limiter`,
`queue_depth_manager`, `product_rules_signal_helper`.

Unit tests for this module are the **first layer** — fast, no Docker, no real Redis.

---

## 3. Test pyramid for Redis

```
                    ┌─────────────┐
                    │   chaos     │  Redis outage mid-flight, recovery SLO
                    │  (slow)     │
               ┌────┴─────────────┴────┐
               │     concurrency       │  Lock races, rate-limit atomicity
               │   (real Redis)        │
          ┌────┴───────────────────────┴────┐
          │         integration             │  Streams, consumer groups, reclaim
          │      (real Redis / Docker)      │
     ┌────┴─────────────────────────────────┴────┐
     │              unit / unit/redis              │  retry logic, config parsing,
     │           (fakeredis or mocks)              │  pure algorithms, mocked failures
     └─────────────────────────────────────────────┘
```

| Layer | Marker | Needs real Redis? | Typical runtime | When to run |
| --- | --- | --- | --- | --- |
| Unit | `@pytest.mark.unit` | No | &lt; 50 ms each | Every push (CI gate) |
| Integration | `@pytest.mark.integration` | Yes (container) | seconds | Every push |
| Concurrency | `@pytest.mark.concurrency` | Yes | seconds | Every push |
| Chaos | `@pytest.mark.chaos` | Yes + fault injection | seconds–minutes | Nightly / pre-release |
| Load / soak | `@pytest.mark.load` / `soak` | Live stack | minutes–hours | On demand |

---

## 4. What to test — concern map

These IDs match [`tests.md`](tests.md) so you can trace reliability goals across the suite.

### 4.1 Unit tests (`unit/redis/`) — logic without infrastructure

Focus: **`redis_stability.py`** and config validation. Use `monkeypatch` + mocks;
no network.

| ID | Concern | File (planned) | What you assert |
| --- | --- | --- | --- |
| R4 | Transient Redis failure | `test_redis_stability.py` | Callable raises `ConnectionError` N−1 times, then succeeds → result returned; `time.sleep` called with bounded delays |
| R5 | Hard Redis failure | `test_redis_stability.py` | Every attempt fails → same exception re-raised after `retry_attempts` |
| — | Backoff jitter | `test_redis_stability.py` | `_retry_delay_s` stays within `[base, max + 20% jitter]` |
| — | Config from env | `test_redis_stability_config.py` | Missing `REDIS_URL` → `RuntimeError`; invalid timeouts → `ValueError` |
| — | Client factory | `test_redis_stability_config.py` | `create_redis_client` passes through pool/timeouts (inspect connection kwargs or mock `from_url`) |
| S7 / L6 | Pool ceiling (smoke) | `test_redis_stability.py` | Documented behavior when `max_connections` exceeded (integration/concurrency owns deep proof) |

**Note:** `global_rate_limiter`, `queue_depth_manager`, and `locks` have their own unit
files under `unit/` (`test_rate_limiter.py`, etc.). The `unit/redis/` folder is
**specifically for shared Redis infrastructure** — client stability, connectivity, and
retry semantics.

### 4.2 Integration tests — real Redis semantics

Requires Docker (testcontainers). Proves Streams, groups, and Lua behave like production.

| ID | Concern | Existing scaffold |
| --- | --- | --- |
| R1–R3 | Reclaim, DLQ, idempotency | `integration/test_reclaim_recovery.py`, `test_idempotency.py` |
| R6 | Lock TTL + safe release | `integration/test_lock_serialization.py` |
| R10 | Duplicate webhook → one job | `integration/test_idempotency.py` |
| R11 / L7 | MAXLEN trim vs pending | `integration/test_stream_consumer_group.py` |
| — | Ingress → stream shape | `integration/test_ingress_enqueue.py` |
| — | Admission 429 gates | `integration/test_admission_gates.py` |

### 4.3 Concurrency tests — races

| ID | Concern | File |
| --- | --- | --- |
| C1–C2 | Per-client lock serialization | `concurrency/test_lock_race.py` |
| C3 | Rate limiter never exceeds RPM | `concurrency/test_rate_limiter_race.py` |
| C4 | One consumer per message | `integration/test_stream_consumer_group.py` |
| C7 | Idempotency SET NX under burst | `concurrency/test_dedup_under_race.py` |

### 4.4 Chaos tests — failure injection

| ID | Concern | File |
| --- | --- | --- |
| R4 | Brief blip → retry → success | `chaos/test_redis_outage.py` |
| R5 | Sustained outage → graceful 5xx | `chaos/test_redis_outage.py` |
| — | Backlog drains after recovery | `chaos/test_redis_outage.py` |

---

## 5. Directory layout

```
dermascan/tests/
├── redis_tests.md                 ← this guide
├── conftest.py                    ← fake_redis, real_redis fixtures
├── fixtures/redis_factory.py      ← factory helpers
│
├── unit/redis/                    ← shared Redis infrastructure (start here)
│   ├── test_redis_stability.py    # R4, R5, backoff/jitter (move from unit/)
│   └── test_redis_stability_config.py  # env parsing + client creation
│
├── unit/                          ← other Redis-backed modules (rate limit, locks, …)
├── integration/                   ← real Redis container
├── concurrency/
└── chaos/
```

**Migration note:** `unit/test_redis_stability.py` is a scaffold today. Step 2 moves it
into `unit/redis/` and implements the first test.

---

## 6. Prerequisites

### 6.1 Python environment

From the repository root:

```powershell
poetry install
poetry shell
```

### 6.2 Test-only dependencies

Installed in `[tool.poetry.group.dev.dependencies]`:

```powershell
poetry install
```

Packages: `fakeredis`, `pytest-asyncio`, `pytest-cov`, `freezegun`, `httpx`, `hypothesis`, `testcontainers`.

(`testcontainers` requires **Docker Desktop** running for integration tests.)

### 6.3 Windows + Docker Desktop

1. **Start Docker Desktop** before integration tests.
2. **Docker CLI on PATH** — if `docker` is not recognized in PowerShell, prepend:

   ```powershell
   $env:Path = "C:\Program Files\Docker\Docker\resources\bin;" + $env:Path
   ```

3. **Ryuk disabled automatically** — `conftest.py` sets `TESTCONTAINERS_RYUK_DISABLED=true`
   because the testcontainers reaper often fails on Windows Docker Desktop.
4. **First run pulls images** (~200 MB):

   ```powershell
   docker pull redis:7
   ```

### 6.4 Environment variables for unit tests

Unit tests **must not** depend on a live Redis server. Patch env in each test:

```python
monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "3")
monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.01")  # fast tests
monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "0.05")
```

Use a high DB index (`/15`) in docs only — unit tests mock operations and never connect.

### 6.5 Environment variables for integration / concurrency

`real_redis` starts an ephemeral Redis 7 container. You do **not** need `REDIS_URL`
pointing at production. Tests inject the container URL via fixtures or monkeypatch
before calling application code.

---

## 7. How to run tests

All commands assume repository root: `c:\Users\LENOVO\Documents\dermascanAgents`

### 7.1 Run only Redis stability unit tests (once implemented)

```powershell
pytest dermascan/tests/unit/redis -m unit -v
```

### 7.2 Run all unit tests (CI gate)

```powershell
pytest dermascan/tests -m unit -v
```

### 7.3 Run integration tests (Docker required)

```powershell
pytest dermascan/tests/integration -m "integration and not slow" -v
```

### 7.4 Run concurrency suite

```powershell
pytest dermascan/tests/concurrency -m concurrency -v
```

### 7.5 Run chaos suite (pre-release)

```powershell
pytest dermascan/tests/chaos -m chaos -v
```

### 7.6 Useful pytest flags

| Flag | Use |
| --- | --- |
| `-k "transient"` | Run tests whose name matches substring |
| `-x` | Stop on first failure |
| `--tb=short` | Shorter tracebacks while learning |
| `-s` | Show print/log output |
| `-m "unit and not slow"` | Combine markers |

### 7.7 Current scaffold status

Many tests still call `pytest.skip("Scaffold: implementation pending")`. Running them
**passes** but reports skips. After implementation, the same commands should report
**passed** with zero skips in the target folder.

---

## 8. Step-by-step implementation plan

We build from the bottom of the pyramid. Each step teaches one concept before adding
infrastructure complexity.

### Step 1 — Read and map ✅

- Read `redis_stability.py` end to end.
- Skim consumers: `global_rate_limiter.py`, `queue_depth_manager.py`, worker startup
  (`check_redis_connectivity` in `orchestrator_worker.py`).

### Step 2 — First unit test: transient retry (R4) ✅

Implemented in `unit/redis/test_redis_stability.py::test_transient_failure_retries_then_succeeds`.

### Step 3 — Hard failure (R5) ✅

Implemented in `test_hard_failure_reraises_after_exhaustion`.

### Step 4 — Backoff bounds ✅

Implemented in `TestRetryDelay` (jitter cap + exponential growth).

### Step 5 — Config loading ✅

Implemented in `unit/redis/test_redis_stability_config.py`.

### Step 6 — Integration: PING against real Redis ✅

Implemented in `integration/test_redis_connectivity.py` (Docker + testcontainers).

### Step 7 — Integration: streams + consumer groups ✅

Implemented in `integration/test_stream_consumer_group.py` with helpers in `fixtures/stream_helpers.py`:

| Test | Concern | What it proves |
| --- | --- | --- |
| `test_xreadgroup_delivers_to_one_consumer` | C4 | One message → one consumer in the group |
| `test_xack_removes_from_pending` | — | XACK clears the pending entries list |
| `test_maxlen_trim_retains_pending` | R11 | Unacked jobs survive approximate trim flood |
| `test_multiple_workers_partition_messages` | S6 | Four workers drain 8 jobs with no duplicates |
| `test_stream_retention_window_documented` | L7 | Exact vs approximate MAXLEN behavior |

Run:

```powershell
poetry run pytest dermascan/tests/integration/test_stream_consumer_group.py -m integration -v
```

### Step 8 — Concurrency + chaos (next)

After happy path is green, add lock/rate-limit races and outage injection.

---

## 9. How to write a unit test — pattern reference

Every Redis stability unit test follows this shape:

```python
"""Unit tests for run_with_redis_retries — concern R4."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from dermascan.shared import redis_stability


@pytest.mark.unit
class TestRunWithRedisRetries:
    def test_transient_failure_retries_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/15")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_ATTEMPTS", "3")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_BASE_S", "0.01")
        monkeypatch.setenv("REDIS_OPERATION_RETRY_MAX_S", "0.05")
        monkeypatch.setattr(redis_stability.time, "sleep", lambda _: None)

        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RedisConnectionError("simulated blip")
            return "ok"

        result = redis_stability.run_with_redis_retries(
            flaky,
            operation_name="test_op",
        )

        assert result == "ok"
        assert calls["n"] == 3
```

**Why each piece exists:**

| Piece | Reason |
| --- | --- |
| `monkeypatch.setenv` | `load_redis_stability_config()` reads env on every retry wrapper call |
| Fast retry env vars | Avoid multi-second sleeps in CI |
| Patch `time.sleep` | Retries stay deterministic and fast |
| Raise `RedisConnectionError` | Matches what `run_with_redis_retries` catches |
| Count calls | Proves retry happened exactly N−1 times before success |

---

## 10. Environment variable reference

| Variable | Default | Validated rule |
| --- | --- | --- |
| `REDIS_URL` | *(required)* | Non-empty |
| `REDIS_SOCKET_TIMEOUT_S` | `10` | &gt; 0 |
| `REDIS_SOCKET_CONNECT_TIMEOUT_S` | `5` | &gt; 0 |
| `REDIS_HEALTH_CHECK_INTERVAL_S` | `30` | ≥ 0 |
| `REDIS_MAX_CONNECTIONS` | `200` | ≥ 1 |
| `REDIS_RETRY_ON_TIMEOUT` | `true` | bool string |
| `REDIS_SOCKET_KEEPALIVE` | `true` | bool string |
| `REDIS_OPERATION_RETRY_ATTEMPTS` | `4` | ≥ 1 |
| `REDIS_OPERATION_RETRY_BASE_S` | `0.25` | &gt; 0 |
| `REDIS_OPERATION_RETRY_MAX_S` | `2.0` | &gt; 0 |

---

## 11. Pass / fail criteria (Redis-specific)

Before production deploy, the Redis suite should demonstrate:

| Criterion | Evidence |
| --- | --- |
| **Transient blips recover** | R4 unit + chaos tests green |
| **Hard outages surface clearly** | R5 unit + chaos tests green; workers log and exit startup on failed `check_redis_connectivity` |
| **No silent over-admission** | C3 concurrency + rate limiter unit tests |
| **No double stream delivery** | C4 integration test |
| **Crash recovery** | R2 reclaim integration + chaos worker kill |
| **Pool exhaustion is bounded** | S7/L6 documented; no indefinite hang under load probe |
| **Fail-open paths stay safe** | R8 product rules helper continues when Redis read fails (warn, don't crash) |

---

## 12. What to do next

1. Run the full Redis suite locally:

   ```powershell
   poetry run pytest dermascan/tests/unit/redis dermascan/tests/integration/test_redis_connectivity.py -v
   ```

2. **Step 8:** Concurrency lock/rate-limit races, then chaos outage tests.

Current status: **21 passed, 1 skipped** across unit/redis + connectivity + stream integration.

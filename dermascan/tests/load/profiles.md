# Load & soak profiles

Documented ramp, spike, and soak scenarios for Locust/k6 runs against a live Dermascan stack.

## Prerequisites

- Live FastAPI gateway + Redis + workers (or mock inference layer)
- `DERMASCAN_INTERNAL_SECRET` set for webhook auth
- Base URL: `http://localhost:8088` (override via `--host` or `DERMASCAN_API_URL`)

## Profiles

### 1. Ramp (discover L1 — max sustainable RPS)

| Step | Duration | Target RPS | Pass criteria |
| --- | --- | --- | --- |
| Warm-up | 2 min | 10 | error rate < 0.1% (excl. intentional 429) |
| Ramp | 10 min | 10 → 500 | p95 ingress < 300 ms until throttle onset |
| Sustain | 5 min | at ceiling | error rate < 1%; 429s only from admission |

**Output:** documented RPS ceiling (L1).

### 2. Spike

| Step | Duration | Target RPS | Pass criteria |
| --- | --- | --- | --- |
| Baseline | 2 min | 50 | stable |
| Spike | 30 s | 10× baseline | graceful 429s, no 5xx storm |
| Recovery | 2 min | 50 | backlog drains, latency normalizes |

### 3. Soak (S8 — memory stability)

| Step | Duration | Target RPS | Pass criteria |
| --- | --- | --- | --- |
| Steady | 30–60 min | 70% of L1 ceiling | RSS flat; no unbounded stream/key growth |

## SLO placeholders (tune from baseline)

- Availability: success rate >= **99%** (excluding intentional 429s)
- Ingress p95 < **300 ms**
- Zero silent drops; zero double-processing
- Recovery: backlog → 0 within **N** seconds after fault

## Run commands

```bash
# Ramp profile (manual stages via Locust UI or headless)
locust -f dermascan/tests/load/locustfile.py --host http://localhost:8088

# Scenario modules (when implemented)
# python -m dermascan.tests.load.scenarios.ingress_only
```

# Dermascan — Engineering Review

> Review date: **2026-10-19** · Reviewed at commit `1972739`.
> Scope: whole backend (`dermascan/`) — ingress API, Redis-stream workers, orchestrator
> agent + tools, model-chain routing, MedGemma Modal service, rules engine, billing.
> This supersedes the previous reliability-only draft; the earlier P0 gaps (stream
> reclaim, outbound retry, base64-on-stream, cooperative shutdown) are now **closed**,
> so the focus moves to code-structure debt, observability depth, and scale ceilings.

---

## 0. Executive scorecard

| Area | Grade | One-line verdict |
|---|---|---|
| 1. Code quality / readability / maintainability | **7.0 / 10** | Strong typing, docstrings, tests; dragged down by a few 2.7k–3.4k-line modules. |
| 2. Architecture / resilience / scalability / reliability | **7.5 / 10** | Crash-recovery + fault-tolerance are genuinely good; single Redis is the ceiling. |
| 3. Performance / efficiency | **7.0 / 10** | Micro-batching, blob refs, pooling in place; queue signal + some O(n log n) compaction remain. |
| 4. Flexibility / extensibility | **8.0 / 10** | Env-driven flags, model-chain abstraction, pluggable channels make change cheap. |

**Overall: 7.4 / 10 — production-capable for thousands of users; a focused push on
observability, module decomposition, and Redis sharding is what unlocks 1M-scale.**

The pipeline is a stream-oriented agentic system:

```
Telegram / WhatsApp (Turn.io)
      │ webhook
      ▼
FastAPI ingress ──XADD──► Redis Streams ──► orchestrator worker ──► MedGemma gateway ──► Modal GPU
   (dedupe,                (jobs/signals,        (LangGraph agent,      (enqueue)           (vLLM batch)
    blob store)             DLQs)                 intake tools)              │
                                                      │                     ▼
                                                      │               medgemma:jobs ──► medgemma worker
                                                      ▼                                      │ result+signal
                                          outbound:messages ◄───────────────────────────────┘
                                                      ▼
                                          outbound sender (compaction + retry) ──► channel API
```

---

## 1. Code quality, readability & maintainability — 7.0 / 10

### Strengths
- **Consistent strict typing and docstrings.** Modules follow the repo standard: top-level
  purpose, typed signatures, Args/Returns/Raises. Good examples: `shared/heartbeat.py`,
  `shared/redis_stability.py`, `backend/utils/backoff.py`, `shared/product_report_enrichment.py`.
- **Clear separation of concerns in `shared/`.** Cross-cutting utilities (Redis stability,
  heartbeat, intake session, delivery context, tracing) are isolated and individually testable.
- **Broad test suite — 92 test files** spanning `unit`, `integration`, `e2e`, `concurrency`,
  `chaos`, and `load`. Reliability-critical logic has dedicated tests
  (`test_reclaim_logic.py`, `test_circuit_breaker.py`, `test_heartbeat_interval.py`,
  `chaos/test_worker_kill.py`, `chaos/test_redis_outage.py`).
- **Errors are explicit and observable.** `run_with_redis_retries`, circuit breakers, and
  DLQ routing raise/record rather than swallow. Few bare `except: pass` sites.
- **Documentation lives with the code** (`heartbeat.md`, `decision_confidence.md`,
  `observability.md`, `billing/README.md`), which keeps intent discoverable.

### Problems (ranked)
1. **Oversized modules violate the 1500-line guideline and concentrate risk.**
   | Module | Lines |
   |---|---|
   | `backend/services/tools/orchestrator/orchestrator_tools.py` | **3382** |
   | `backend/services/agents/orchestrator_worker.py` | **2728** |
   | `backend/services/agents/product_rules_signal_helper.py` | 1504 |
   | `backend/services/agents/model_fallback_chain.py` | 1309 |
   | `backend/api/endpoints.py` | 1279 |
   | `rules_engine/engine/rules_engine.py` | 1157 |

   `orchestrator_tools.py` and `orchestrator_worker.py` are the two highest-churn,
   highest-risk files and both are ~2× the limit. They mix tool definitions, intake
   state machine, enqueue/dispatch, billing hooks, and signal handling.
   *Fix:* split `orchestrator_tools.py` into `intake_tools.py`, `image_tools.py`,
   `enqueue_dispatch.py`, `intake_state.py`; split `orchestrator_worker.py` into
   reader-loop, job-handler, signal-handler, and coordination modules.

2. **Deeply nested handler functions** (e.g. `_collect_batch`, `_process_signal_message`,
   the enqueue path) run to hundreds of lines with inline closures and multi-level `try`
   blocks. They are correct but hard to review; extracting named helpers would cut cognitive load.

3. **Minor inconsistencies:** two logging styles coexist (`get_logger` structured + raw
   `logging.basicConfig` in `do_work.py`); a `__main__` smoke block is left in
   `product_rules_signal_helper.py`; occasional typos in comments. Low severity, easy cleanup.

4. **Duplicated TLS/verify + retry wiring** across `outbound_sender_worker.py` and
   `api/endpoints.py` (each re-parses `*_TLS_VERIFY`). Centralize in one channel-client helper.

---

## 2. Architecture, resilience, scalability & reliability — 7.5 / 10

### What is now solid (closed since the last review)
- **Stream crash-recovery.** `reclaim_logic.py` (`reclaim_stale_pending` via `XAUTOCLAIM`),
  idempotency markers (`mark_processed`/`is_processed`), reclaim-attempt caps, and DLQ routing,
  wired into every worker through background reclaim threads.
- **Liveness heartbeat.** `heartbeat.py` (`start_stream_heartbeat` → `XCLAIM JUSTID min-idle=0`)
  keeps long, healthy turns from being reclaimed as dead — used in orchestrator, indexing, and
  MedGemma workers. This closed the cold-start duplicate-job class of bug.
- **Per-client coordination.** Distributed `client_lock` (SET NX PX + Lua CAS release) plus a
  **lock-lease extender thread** serialize same-client turns across processes without deadlock.
- **Outbound delivery retry (previous #1 gap — fixed).** `backoff.py::post_with_outbound_retry`
  retries 429/5xx/timeouts/connection errors with jittered exponential backoff and honors
  `Retry-After`; DLQ only after exhaustion.
- **Transient-fault tolerance everywhere.** `redis_stability.py` (hardened pool + keepalive +
  `run_with_redis_retries`), MedGemma `_dispatch_with_retry`, and `provider_circuit_breaker.py`.
- **Dual-layer model resilience (new).** `model_chain_routing.py` (Layer 0: Gemini-first vs
  OpenAI-first based on a Redis degradation signal, with per-turn stickiness) over
  `model_fallback_chain.py` (Layer 1: walk the chain, per-model breakers, 499/504 fallback).
- **Cooperative shutdown.** `do_work.py` supervises worker threads on a shared `stop_event`
  and traps `SIGINT`/`SIGTERM`.
- **Durable-store separation (new).** Billing moved to **Postgres** with a real pooled client
  (`billing/db.py`: `ConnectionPool`, keepalives, pre-checkout liveness check), removing that
  load from Redis and fixing the Cloud Run idle-socket failures.

### Remaining architectural risks (ranked)
1. **Single Redis is still the dominant scale ceiling.** Streams, locks, inflight markers,
   dedupe, blobs-TTL, checkpoints, and logs share one instance and one failure domain. Hot keys
   (consumer-group reads, `outbound:seq:<client_id>`) serialize at high fan-out.
   *Fix:* split by concern (streams vs cache/locks vs checkpointer), enable Redis Cluster, and
   evaluate Kafka/Pulsar only if per-key ordering at very high fan-out is needed.

2. **Job↔signal ordering still polls.** `_wait_for_inflight` (200 ms × budget) remains, now
   backed by the client lock. It adds a latency floor and a correctness-on-TTL edge.
   *Fix (interim):* Redis Pub/Sub notify instead of sleep-poll. *Fix (target):* partition
   streams by `client_id` so job+signal for a client are processed single-writer by construction
   — this also deletes the lock.

3. **Observability is structural but not numeric.** LangSmith agent + pipeline tracing and the
   local `agent_call_tracer` give excellent per-turn forensics, but there are **no counters /
   histograms / alerts** (no `/metrics`, no Prometheus/StatsD). You can debug one turn but cannot
   see stream depth, oldest-pending age, per-handler P95, DLQ growth, or outbound failure rate in
   aggregate. This is the top operational gap for scale.

4. **Queue-pressure signal is single-measure.** `queue_depth_manager` reads `XPENDING.pending`
   only; it can read "green" while `XLEN` backlog and oldest-pending age climb.
   *Fix:* combine `XLEN` + pending + oldest-age, promote to red if any breaches budget.

5. **Security default:** `TELEGRAM_TLS_VERIFY` defaults to **`false`** in
   `outbound_sender_worker.py` (ingress `endpoints.py` correctly defaults `true`). Make verify
   mandatory in the production profile and align the two defaults.

6. **No DLQ replay tooling.** DLQs are well-modeled per domain but replay is manual; add a gated
   `xrange dlq → xadd source` CLI and alert on DLQ write-rate.

---

## 3. Performance & efficiency — 7.0 / 10

### Strengths
- **MedGemma micro-batching** (`MEDGEMMA_STREAM_BATCH_SIZE`, 2 s window, bounded inflight)
  approximates one forward pass per batch — correct lever, since GPU latency dominates cost.
- **Images travel as blob refs, not base64.** The worker resolves bytes via
  `download_client_image_bytes_by_path`; base64 remains only as a legacy migration fallback.
  This removed the earlier CPU/Redis amplifier.
- **Connection reuse:** hardened Redis pool with keepalives; billing Postgres pool with
  pre-checkout checks; capacity-isolated orchestrator readers (jobs vs signals) on a shared pool.
- **Bounded upstreams:** vLLM/Modal calls are time-boxed and retried; no `timeout=None` hangs.

### Opportunities (ranked)
1. **Instrument before tuning.** `MAX_INFLIGHT`, batch windows, and reclaim idle thresholds are
   currently set by reasoning, not measured drain rate. The metrics bundle (area 2, item 3) is a
   prerequisite to size these from data.
2. **Outbound compaction cost.** `_delivery_order_key` + per-case/per-turn sorting and
   dict-building in `outbound_sender_worker.py` is fine for small batches but is `O(n log n)` with
   per-item allocation; profile under burst load before raising batch sizes.
3. **Per-message `events:stream` writes** roughly double Redis write load; sample or aggregate
   non-critical events.
4. **LangGraph checkpoint on Redis per turn** adds read/write amplification on the shared
   instance; moving the checkpointer to its own Redis is part of the split in area 2.
5. **Rules-engine fan-out** (per-ingredient indexing + embeddings + Pinecone upsert) is already
   offloaded to an async index worker — good; keep that path off the hot orchestrator threads.

---

## 4. System flexibility & extensibility — 8.0 / 10

### Strengths
- **Config-as-flags.** Behavior is overwhelmingly env-driven: model chains
  (`ORCHESTRATOR_MODEL_CHAIN_ALT`, `MODEL_CHAIN_PREFER_PRIMARY`), report budgets
  (`SIGNAL_REPORT_*`), brand priority (`PRODUCT_RECOMMENDATION_PRIORITY_BRANDS`), cosmetic
  feature flags, heartbeat/reclaim cadences, TLS. New environments change config, not code.
- **Model abstraction is a standout.** `ModelChainRunner` + `build_model_chain_from_spec` make
  provider/model order a data decision; adding or reordering providers needs no handler changes.
- **Channel-agnostic core.** Telegram and WhatsApp (Turn.io) share the stream + outbound
  contract; a new channel is an ingress adapter + an outbound sender branch.
- **Pluggable report/skin logic.** Prompt+format helpers (`product_report_enrichment.py`,
  `concise_report_format.py`, `decision_confidence.py`) isolate user-facing copy/structure from
  flow control, so report shape evolves without touching routing.
- **Cosmetic-pivot flags** let whole routing behaviors toggle (skin-first, text-only product
  review, consent escalation) per environment.

### Limits
1. **Flexibility concentrates in the giant modules.** Because intake tools and the orchestrator
   loop are monolithic, *adding a tool or a routing branch* still means editing 3k-line files —
   the decomposition in area 1 is what would make this a 9.
2. **Flag sprawl without a registry.** Dozens of env flags with ad-hoc parsing/defaults scattered
   across modules; a typed settings object (pydantic-settings) would centralize validation and
   document the surface.
3. **Hard-coded provider domains** in some search tools (INCI/medical allow-lists) are inline;
   lifting them to config would ease regional/market variations.

---

## 5. Prioritized roadmap

**Phase 1 — operability (1–2 weeks, highest ROI)**
- Ship the **metrics bundle**: per-stream depth/pending/oldest-age, per-handler P50/P95/P99 +
  error rate, per-dependency success/latency/retry, DLQ growth; wire alerts. Tune everything after.
- Enforce **`TELEGRAM_TLS_VERIFY=true`** in the prod profile; align the two defaults.
- Combine **XLEN + pending + oldest-age** in `queue_depth_manager`.

**Phase 2 — maintainability (2–4 weeks)**
- Decompose `orchestrator_tools.py` and `orchestrator_worker.py` below the 1500-line limit.
- Introduce a typed settings registry for env flags.
- Add a gated **DLQ replay CLI** with first-failure metadata.

**Phase 3 — scale ceilings (4–8 weeks)**
- **Split Redis by concern** (streams / locks+cache / checkpointer) and enable Redis Cluster.
- Replace `_wait_for_inflight` polling with **Pub/Sub notify**, then **partition streams by
  `client_id`** to remove the lock and get per-key ordering for free.
- Load-test to find the knee; size `MAX_INFLIGHT` / batch windows from measured drain rate.
- Autoscale GPU inference on queue depth + oldest-age with warm replicas to bound cold starts.

---

## 6. Bottom line

The reliability story that dominated the previous review is **largely solved**: the system now
self-heals from crashes (reclaim + heartbeat), survives transient faults (Redis/HTTP/provider
retries + breakers), retries the last outbound hop, keeps images off the stream, and has moved
billing to a properly pooled Postgres. The remaining work is **not** firefighting — it is
**operational maturity** (numeric observability), **structural hygiene** (break up the two
monolith modules), and **horizontal scale** (shard/cluster Redis, partition by client). Execute
Phases 1–3 and this comfortably becomes an 8.5+/10, million-user-ready platform.

---

*End of `claude_review.md`.*

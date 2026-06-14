# Escalation

How low-confidence or malformed inference results leave the AI pipeline and reach a human dermatologist, then return to the client via the signal stream.

**Source of truth:** `dermascan/ai_service/postprocessing/escalation.py` → `route_case_after_inference`

---

## 1. When a case is escalated

After MedGemma inference, `route_case_after_inference` parses the model JSON and decides routing.

| Condition | Result |
|-----------|--------|
| Confidence below threshold | Escalate |
| Model output is not valid JSON | Escalate |
| Confidence at or above threshold | Complete normally (no escalation job) |

**Thresholds** (env):

| Mode | Env var | Default |
|------|---------|---------|
| Skin / diagnose | `DIAGNOSE_CONFIDENCE_THRESHOLD` | `0.75` |
| Product review | `PRODUCT_REVIEW_CONFIDENCE_THRESHOLD` | `0.60` |

**Mode detection** (in order):

1. `review_type` in model JSON (`skin review`, `product review`, `hybrid review`, …)
2. Else infer from fields: both diagnosis + product fields → `hybrid`; product only → `product_review`; diagnosis only → `diagnose`
3. Fallback → `diagnose`

---

## 2. Streams involved

Every post-inference case writes to **three** places. Escalated cases write to all three; non-escalated cases skip the escalation stream.

```
MedGemma worker
      │
      ▼
route_case_after_inference
      │
      ├──► events:stream          (audit / lifecycle)
      ├──► escalation:jobs        (human review queue)     ← only when escalated
      └──► orchestrator:signals   (proactive client update)
```

| Stream | Env var | Default | Purpose |
|--------|---------|---------|---------|
| Events | `EVENTS_STREAM` | `events:stream` | Immutable audit trail |
| Escalation | `ESCALATION_STREAM` | `escalation:jobs` | Jobs for dermatologist platform |
| Signal | `ORCHESTRATOR_SIGNAL_STREAM` | `orchestrator:signals` | Orchestrator proactive delivery |

**Stream rules** (same as all other streams):

- Redis `XADD` — top-level fields are **flat strings**
- Nested data lives in a JSON string field (`payload` on signal stream)
- Use `maxlen` + `approximate=True` (see `WORKERS_STREAM_MAXLEN`, default `5000`)
- Failed consumers → DLQ (`ESCALATION_DLQ_STREAM`, default `escalation:dlq`)

---

## 3. Base fields

These fields are shared across events, escalation jobs, and signal payloads. They come from parsed MedGemma JSON (`_extract_structured_fields`).

| Field | Type | Description |
|-------|------|-------------|
| `review_type` | string | e.g. `skin review`, `product review` |
| `diagnosis` | string | Primary skin finding |
| `rationale` | string | Model reasoning |
| `summary` | object | `{ severity, active_inflammation, barrier_status, observed_patterns[] }` |
| `treatment` | object | `{ needs{…}, contraindicated_approaches[] }` |
| `product_name` | string | Product label |
| `description` | string | Product description |
| `indications` | string | Intended use |
| `safety_in_pregnancy` | string | Pregnancy safety note |
| `contraindications` | string | Contraindications |
| `how_to_use` | string | Usage instructions |
| `special_instructions` | string | Extra cautions |

**Case identity** (on every stream entry):

| Field | Description |
|-------|-------------|
| `request_id` | Ingress request id |
| `case_id` | Case id (usually same as request) |
| `client_id` | Client / thread id |
| `message_id` | Channel message id |
| `channel` | e.g. `telegram`, `whatsapp` |

---

## 4. Escalation stream schema

**Stream:** `escalation:jobs`  
**Producer:** `_push_escalation` in `escalation.py`  
**Consumer:** Escalation worker (see **§5**)

Flat string fields on each job:

| Field | Example | Notes |
|-------|---------|-------|
| `request_id` | `req_abc123` | |
| `case_id` | `req_abc123` | |
| `client_id` | `tg:123456` | Use to fetch conversation context |
| `message_id` | `msg_789` | |
| `channel` | `telegram` | |
| `source_agent` | `medgemma` | |
| `mode` | `diagnose` | `diagnose` \| `product_review` \| `hybrid` |
| `reason` | `low_confidence` | Also set when JSON parse fails (still `low_confidence` today) |
| `confidence` | `0.62` | Empty string if missing |
| `threshold` | `0.75` | Threshold used for this mode |
| `diagnosis_text` | `Acne vulgaris…` | `diagnosis` field, or raw model text as fallback |
| `requires_human_review` | `true` | Always `true` on this stream |
| `product_name` | `…` | From base fields |
| `description` | `…` | |
| `indications` | `…` | |
| `safety_in_pregnancy` | `…` | |
| `contraindications` | `…` | |
| `how_to_use` | `…` | |
| `special_instructions` | `…` | |

> **Note:** `summary`, `treatment`, and `rationale` are not on the escalation stream today. The consumer should attach them from the matching `events:stream` entry or re-read from the events payload if needed for the dermatologist UI.

---

## 5. Escalation worker standard

**File:** `dermascan/backend/services/agents/escalation_worker.py` (scaffold today)  
**Reference implementations:** `knowledge_worker.py`, `outbound_sender_worker.py`, `medgemma_service/worker.py`

The escalation worker follows the same Redis stream consumer pattern as every other worker in this repo. It is **not** started by `do_work.py` today (same as the knowledge worker) — run it as its own process.

### 5.1 Responsibilities

| Phase | What the worker does |
|-------|----------------------|
| **Consume** | Read jobs from `escalation:jobs` via consumer group |
| **Enrich** | Attach last N conversation turns from `orchestrator:conversation_log:{client_id}` |
| **Relay** | POST handoff packet to the dermatologist platform (with retries) |
| **Audit** | Emit lifecycle events to `events:stream` at each step |
| **Return** | When the platform sends physician notes back, publish `{mode}_physician_reviewed` to `orchestrator:signals` (payload unchanged + `derma_notes` only) |

### 5.2 Configuration

**Escalation-specific env**

| Variable | Default | Purpose |
|----------|---------|---------|
| `ESCALATION_STREAM` | `escalation:jobs` | Input job stream |
| `ESCALATION_DLQ_STREAM` | `escalation:dlq` | Poison / exhausted-retry jobs |
| `ESCALATION_CONSUMER_GROUP` | `escalation-workers` | Redis consumer group |
| `ESCALATION_CONSUMER_NAME` | `escalation-worker` | Base name; suffix `{hostname}-{pid}` at runtime |
| `ESCALATION_RELAY_MAX_ATTEMPTS` | `3` | Platform relay retries |
| `ESCALATION_RELAY_BASE_DELAY_SEC` | `0.75` | Exponential backoff base |
| `ESCALATION_CONTEXT_MESSAGE_N` | `10` | Conversation turns to attach (falls back to `CONVERSATION_LOG_INCLUDE_N`) |
| `ESCALATION_PLATFORM_URL` | *(required)* | Dermatologist platform ingest URL |
| `ESCALATION_PLATFORM_API_KEY` | *(optional)* | Auth header for platform calls |

**Shared worker env** (same as knowledge / outbound / medgemma)

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | — | Required |
| `EVENTS_STREAM` | `events:stream` | Lifecycle audit |
| `ORCHESTRATOR_SIGNAL_STREAM` | `orchestrator:signals` | Physician return path |
| `WORKERS_STREAM_MAXLEN` | `5000` | `XADD` maxlen on all writes |
| `WORKERS_STREAM_BLOCK_MS` | `500` | `XREADGROUP` block timeout |
| `WORKERS_STREAM_BATCH_SIZE` | `8` | Messages per read |
| `WORKERS_MAX_INFLIGHT` | `8` | Thread-pool concurrency |
| `WORKERS_STREAM_PENDING_MIN_IDLE_MS` | `120000` | Reclaim idle threshold |
| `WORKERS_RECLAIM_INTERVAL_SEC` | `30` | Reclaim loop interval |
| `WORKERS_MAX_RECLAIM_ATTEMPTS` | `3` | Reclaim → DLQ cutoff |
| `CONVERSATION_LOG_KEY_PREFIX` | `orchestrator:conversation_log` | Context source |

### 5.3 Job payload model

Parse stream fields into a typed model (same pattern as `KnowledgeJobPayload`):

```python
class EscalationJobPayload(BaseModel):
    schema_version: str = "v1"

    # identity
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str

    # routing
    source_agent: str = "medgemma"
    mode: str                          # diagnose | product_review | hybrid
    reason: str = "low_confidence"
    confidence: float | None = None
    threshold: float | None = None
    requires_human_review: bool = True

    # model output (flat strings from stream)
    diagnosis_text: str = ""
    product_name: str = ""
    description: str = ""
    indications: str = ""
    safety_in_pregnancy: str = ""
    contraindications: str = ""
    how_to_use: str = ""
    special_instructions: str = ""
```

### 5.4 Startup (`run_worker_forever`)

```
1. load_dotenv()
2. require REDIS_URL
3. redis client (decode_responses=True)
4. reclaim_logic.set_redis_client(_redis)
5. _ensure_consumer_group()          # XGROUP CREATE … mkstream=True; ignore BUSYGROUP
6. start reclaim background thread   # _start_reclaim_thread(stop_event)
7. enter main XREADGROUP loop
8. on KeyboardInterrupt: stop_event → join reclaim thread
```

**Consumer name:** `f"{ESCALATION_CONSUMER_NAME}-{socket.gethostname()}-{os.getpid()}"`

### 5.5 Main read loop

Same shape as `knowledge_worker.run()`:

```
while True:
    resp = redis.xreadgroup(
        groupname=ESCALATION_CONSUMER_GROUP,
        consumername=ESCALATION_CONSUMER_NAME,
        streams={ESCALATION_STREAM: ">"},
        count=WORKERS_STREAM_BATCH_SIZE,
        block=WORKERS_STREAM_BLOCK_MS,
    )
    for msg_id, fields in messages:
        pool.submit(_process_escalation_job, msg_id, fields)
```

Use `ThreadPoolExecutor(max_workers=WORKERS_MAX_INFLIGHT)` for concurrent jobs.

On `xreadgroup` Redis errors: log, sleep 250 ms, continue (do not crash the process).

### 5.6 Process one job (`_process_escalation_job`)

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Parse fields → EscalationJobPayload                      │
│ 2. Emit event: escalation_job_consumed                      │
│ 3. context = _read_conversation_log(client_id, N)           │
│ 4. handoff = build_handoff_packet(payload, context)         │
│ 5. Relay to platform (retry loop, see §5.7)                 │
│ 6. On success:                                              │
│      - cache original signal payload for return path        │
│      - emit escalation_relay_completed                      │
│      - XACK + XDEL + mark_processed                         │
│ 7. On unrecoverable failure:                                │
│      - XADD escalation:dlq                                  │
│      - emit escalation_relay_failed                         │
│      - XACK + XDEL                                          │
└─────────────────────────────────────────────────────────────┘
```

**Cache for return path** (so physician signal can copy the original payload):

| Redis key | Value | TTL |
|-----------|-------|-----|
| `escalation:signal_payload:{case_id}` | JSON of the `{mode}_escalated` signal `payload` | 7 days |

Populate this from the matching `events:stream` row or by reconstructing from job fields + base fields at relay time.

### 5.7 Platform relay with retries

Same retry pattern as `knowledge_worker._process_knowledge_job`:

```
for attempt in 1..ESCALATION_RELAY_MAX_ATTEMPTS:
    try:
        POST ESCALATION_PLATFORM_URL with handoff JSON
        break on 2xx
    except:
        emit event: escalation_relay_retry (attempt, error, delay)
        sleep ESCALATION_RELAY_BASE_DELAY_SEC * 2^(attempt-1)

if all attempts failed:
    emit event: escalation_relay_failed
    push to DLQ (§5.9)
    optionally enqueue a degraded orchestrator signal so the user is not left silent
```

**Idempotency before relay:** `SET escalation:relayed:{case_id} 1 NX EX 86400` — skip duplicate relay if key exists (still ack the stream message).

### 5.8 Physician return path

Triggered when the dermatologist platform calls back (webhook on FastAPI or internal function invoked by a platform adapter):

```
1. Validate case_id + derma_notes
2. Load cached payload from escalation:signal_payload:{case_id}
3. payload["derma_notes"] = incoming derma_notes   # ONLY addition
4. XADD orchestrator:signals {
       signal_type: "{mode}_physician_reviewed",
       signal_id: uuid,
       request_id, case_id, client_id, message_id, channel,
       source_event_id: escalation job msg_id,
       payload: json.dumps(payload),
       notes: "Physician review completed",
   }
5. Emit event: physician_review_completed
6. DEL escalation:signal_payload:{case_id}   # best-effort cleanup
```

Do **not** re-enter `escalation:jobs` on the return path.

### 5.9 DLQ schema

Written on unrecoverable handler failure (same contract as knowledge / orchestrator DLQs):

| Field | Description |
|-------|-------------|
| `request_id`, `case_id`, `client_id`, `message_id`, `channel` | Case identity |
| `source_stream` | `escalation:jobs` |
| `source_msg_id` | Original stream message id |
| `error` | Exception message |
| `payload` | `json.dumps(original_fields)` |
| `failed_at_ms` | Unix ms timestamp |

After DLQ write: **always** `XACK` + `XDEL` the source message so it does not stay pending forever.

### 5.10 Reclaim thread (stale pending recovery)

Use `reclaim_logic.reclaim_stale_pending` — identical to medgemma / outbound / orchestrator:

```
def _start_reclaim_thread(stop_event):
    loop every WORKERS_RECLAIM_INTERVAL_SEC:
        reclaim_stale_pending(
            stream=ESCALATION_STREAM,
            group=ESCALATION_CONSUMER_GROUP,
            consumer=ESCALATION_CONSUMER_NAME,
            handler=_reclaim_handler,
            dlq_stream=ESCALATION_DLQ_STREAM,
        )

def _reclaim_handler(msg_id, fields):
    if mark_processed(ESCALATION_STREAM, msg_id):   # idempotency gate
        try:
            _process_escalation_job(msg_id, fields)
        except:
            delete processed marker; leave pending for next reclaim
```

Messages exceeding `WORKERS_MAX_RECLAIM_ATTEMPTS` reclaim cycles are auto-written to DLQ by `reclaim_logic` with:

| Field | Value |
|-------|-------|
| `original_stream` | `escalation:jobs` |
| `original_msg_id` | stream id |
| `original_group` | consumer group |
| `reclaim_attempts` | count |
| `reason` | `max_reclaim_attempts_exceeded` |
| *original job fields* | copied flat |

### 5.11 Events emitted by the worker

| `status` | When |
|----------|------|
| `escalation_job_consumed` | Job parsed and accepted |
| `escalation_relay_retry` | Platform POST failed, will retry |
| `escalation_relay_completed` | Platform accepted the case |
| `escalation_relay_failed` | Retries exhausted or poison payload |
| `physician_review_completed` | `{mode}_physician_reviewed` signal published |

All events include: `request_id`, `case_id`, `client_id`, `message_id`, `channel`, `agent: "escalation"`, `mode`.

### 5.12 Success / failure invariants

| Rule | Why |
|------|-----|
| Never leave a processed message unacked | Prevents pending pile-up |
| Always `XDEL` after `XACK` on success or DLQ | Keeps stream lean (same as orchestrator) |
| `mark_processed` after successful handling | Reclaim idempotency |
| DLQ before ack on poison jobs | Audit trail for manual replay |
| Flat string values in `XADD` | Redis stream contract |
| `maxlen=WORKERS_STREAM_MAXLEN, approximate=True` | Bounded memory |

### 5.13 Worker flow diagram

```
escalation:jobs
      │
      ▼
XREADGROUP (escalation-workers)
      │
      ▼
_process_escalation_job
      │
      ├─ read conversation_log (last N)
      ├─ POST → dermatologist platform (retry)
      ├─ cache signal payload in Redis
      ├─ events:stream (lifecycle)
      │
      ├─ success ──► XACK + XDEL + mark_processed
      │
      └─ failure ──► escalation:dlq + XACK + XDEL

[async] Platform callback with derma_notes
      │
      ▼
orchestrator:signals ({mode}_physician_reviewed)
      │
      ▼
orchestrator_worker → outbound:messages → client
```

---

## 6. Events stream (reference)

Two events per escalated case:

**1. Inference completed** — always emitted

| Field | Value |
|-------|-------|
| `agent` | `medgemma` |
| `mode` | `diagnose` / `product_review` / `hybrid` |
| `status` | `{mode}_inference_completed` |
| `confidence`, `threshold` | strings |
| `escalation` | `true` or `false` |
| `escalation_note` | Human-readable reason (empty if not escalated) |
| *base fields* | All base fields as flat strings; `summary` / `treatment` JSON-stringified |

**2. Escalated** — only when escalated

| Field | Value |
|-------|-------|
| `status` | `{mode}_escalated` |
| `escalation` | `true` |
| *base fields* | Same as above (no `escalation_note` on this second event) |

---

## 7. Signal stream (reference)

**Stream:** `orchestrator:signals`  
**Producer:** `_push_signal` in `escalation.py`

### Top-level envelope (same as every signal)

| Field | Description |
|-------|-------------|
| `signal_type` | `{mode}_escalated` or `{mode}_completed` |
| `signal_id` | MedGemma job message id |
| `request_id`, `case_id`, `client_id`, `message_id`, `channel` | Case identity |
| `source_event_id` | Empty today |
| `payload` | JSON string (see below) |
| `notes` | Short human-readable status for the orchestrator |

### `payload` JSON (escalated example)

```json
{
  "review_type": "skin review",
  "diagnosis": "Acne vulgaris, moderate",
  "diagnosis_text": "Acne vulgaris, moderate",
  "rationale": "…",
  "summary": {
    "severity": "moderate",
    "active_inflammation": "yes",
    "barrier_status": "compromised",
    "observed_patterns": ["papules", "comedones"]
  },
  "treatment": {
    "needs": {
      "barrier_repair": "yes",
      "anti_inflammatory": "yes",
      "comedolytic": "yes",
      "hydration": "yes",
      "sun_protection": "yes"
    },
    "contraindicated_approaches": []
  },
  "confidence": 0.62,
  "threshold": 0.75,
  "escalation": true,
  "mode": "diagnose",
  "product_name": "",
  "description": "",
  "indications": "",
  "safety_in_pregnancy": "",
  "contraindications": "",
  "how_to_use": "",
  "special_instructions": "",
  "metadata": {
    "extras": {
      "group_id": "",
      "group_expected_count": "1"
    }
  }
}
```

When not escalated: same shape, `"escalation": false`, `signal_type` = `{mode}_completed`.

---

## 8. Extract last N messages for dermatologist context

When the escalation consumer reads a job from `escalation:jobs`, it should attach recent chat context before relaying to the dermatologist platform.

### Where messages live

| Redis key | Pattern |
|-----------|---------|
| Conversation log | `orchestrator:conversation_log:{client_id}` |

Env: `CONVERSATION_LOG_KEY_PREFIX` (default `orchestrator:conversation_log`)

### How to read (same pattern as orchestrator signal flow)

1. Take `client_id` from the escalation job.
2. `LRANGE orchestrator:conversation_log:{client_id} 0 N-1` — **newest first**.
3. Parse each entry as JSON.

**Recommended N:** `10` (orchestrator rules flows use `10`; default config `CONVERSATION_LOG_INCLUDE_N` = `8`).

### Entry shape

```json
{
  "ts_ms": 1718366400000,
  "role": "user",
  "text": "Can you look at this rash on my cheek?",
  "request_id": "req_abc123",
  "case_id": "req_abc123"
}
```

`role` is `user` or `assistant`.

### Handoff packet (what to send the dermatologist platform)

Combine the escalation job fields + conversation context:

```json
{
  "case": {
    "request_id": "…",
    "case_id": "…",
    "client_id": "…",
    "channel": "…",
    "mode": "diagnose",
    "confidence": 0.62,
    "threshold": 0.75,
    "diagnosis_text": "…",
    "product_name": "…",
    "description": "…"
  },
  "conversation_context": [
    { "role": "user", "text": "…", "ts_ms": 1718366400000 },
    { "role": "assistant", "text": "…", "ts_ms": 1718366410000 }
  ]
}
```

Reverse the list (oldest first) if the platform reads top-to-bottom chronologically.

---

## 9. Dermatologist notes → signal stream (return path)

When the dermatologist finishes review, publish **one** entry to `orchestrator:signals`.

**Rule:** Copy the original escalated signal payload exactly. Add **only** `derma_notes`. Do not rename, drop, or reshape existing fields.

### Top-level envelope

Same as any signal:

| Field | Value |
|-------|-------|
| `signal_type` | `{mode}_physician_reviewed` e.g. `diagnose_physician_reviewed` |
| `signal_id` | New uuid |
| `request_id`, `case_id`, `client_id`, `message_id`, `channel` | Same as original case |
| `source_event_id` | Escalation job stream id (optional) |
| `payload` | Original payload + `derma_notes` |
| `notes` | e.g. `"Physician review completed"` |

### `derma_notes` (the only addition)

```json
{
  "physician_id": "dr-jane-smith",
  "reviewed_at_ms": 1718367000000,
  "disposition": "confirmed",
  "summary": "Agree with AI assessment. Moderate inflammatory acne.",
  "clinical_notes": "Recommend gradual retinoid introduction. Patch test if sensitive.",
  "follow_up": "Reassess in 6 weeks if no improvement."
}
```

| Field | Description |
|-------|-------------|
| `physician_id` | Reviewer id on dermatologist platform |
| `reviewed_at_ms` | Unix ms timestamp |
| `disposition` | `confirmed` \| `revised` \| `unable_to_assess` |
| `summary` | Short physician assessment |
| `clinical_notes` | Detailed notes for care team |
| `follow_up` | Optional next steps |

### Full return payload example

Everything from §7, plus one key:

```json
{
  "review_type": "skin review",
  "diagnosis": "Acne vulgaris, moderate",
  "diagnosis_text": "Acne vulgaris, moderate",
  "rationale": "…",
  "summary": { "…": "…" },
  "treatment": { "…": "…" },
  "confidence": 0.62,
  "threshold": 0.75,
  "escalation": true,
  "mode": "diagnose",
  "product_name": "",
  "description": "",
  "indications": "",
  "safety_in_pregnancy": "",
  "contraindications": "",
  "how_to_use": "",
  "special_instructions": "",
  "metadata": { "extras": { "group_id": "", "group_expected_count": "1" } },

  "derma_notes": {
    "physician_id": "dr-jane-smith",
    "reviewed_at_ms": 1718367000000,
    "disposition": "revised",
    "summary": "Inflammatory acne confirmed; severity mild-moderate rather than moderate.",
    "clinical_notes": "Start benzoyl peroxide 2.5% before retinoid. Avoid concurrent harsh scrubs.",
    "follow_up": "Follow up in 4 weeks."
  }
}
```

The orchestrator consumes this like any other signal. It should treat `derma_notes` as authoritative over the AI-only fields when composing the user-facing message.

---

## 10. End-to-end flow

```
User message
     │
     ▼
MedGemma inference
     │
     ▼
route_case_after_inference
     │
     ├─ confidence OK ──► orchestrator:signals ({mode}_completed)
     │                    events:stream (inference_completed, escalation=false)
     │
     └─ confidence LOW ─► escalation:jobs          ──► Escalation worker (§5)
                          orchestrator:signals       │      │
                          ({mode}_escalated)         │      ├─ read conversation_log (last N)
                          events:stream (×2)         │      ├─ relay to dermatologist platform
                                                     │      ├─ on failure → escalation:dlq
                                                     │      └─ reclaim thread (stale pending)
                                                     │
                          ◄──────────────────────────┘
                          orchestrator:signals ({mode}_physician_reviewed)
                          payload = original + derma_notes
                                     │
                                     ▼
                          Orchestrator → outbound:messages → client
```

---

## 11. Env checklist

### Producer (escalation.py)

| Variable | Default |
|----------|---------|
| `REDIS_URL` | `redis://localhost:6379/0` |
| `ESCALATION_STREAM` | `escalation:jobs` |
| `ESCALATION_DLQ_STREAM` | `escalation:dlq` |
| `ORCHESTRATOR_SIGNAL_STREAM` | `orchestrator:signals` |
| `EVENTS_STREAM` | `events:stream` |
| `DIAGNOSE_CONFIDENCE_THRESHOLD` | `0.75` |
| `PRODUCT_REVIEW_CONFIDENCE_THRESHOLD` | `0.60` |

### Worker (escalation_worker.py)

| Variable | Default |
|----------|---------|
| `ESCALATION_CONSUMER_GROUP` | `escalation-workers` |
| `ESCALATION_CONSUMER_NAME` | `escalation-worker` |
| `ESCALATION_RELAY_MAX_ATTEMPTS` | `3` |
| `ESCALATION_RELAY_BASE_DELAY_SEC` | `0.75` |
| `ESCALATION_CONTEXT_MESSAGE_N` | `10` |
| `ESCALATION_PLATFORM_URL` | *(required at runtime)* |
| `WORKERS_STREAM_MAXLEN` | `5000` |
| `WORKERS_STREAM_BLOCK_MS` | `500` |
| `WORKERS_STREAM_BATCH_SIZE` | `8` |
| `WORKERS_MAX_INFLIGHT` | `8` |
| `WORKERS_STREAM_PENDING_MIN_IDLE_MS` | `120000` |
| `WORKERS_RECLAIM_INTERVAL_SEC` | `30` |
| `WORKERS_MAX_RECLAIM_ATTEMPTS` | `3` |
| `CONVERSATION_LOG_KEY_PREFIX` | `orchestrator:conversation_log` |
| `CONVERSATION_LOG_INCLUDE_N` | `8` |

---

## Related files

| File | Role |
|------|------|
| `escalation.py` | Routing, stream producers |
| `escalation_worker.py` | Consumer (implement per §5) |
| `reclaim_logic.py` | Shared stale-pending reclaim + DLQ |
| `knowledge_worker.py` | Reference: retry loop, DLQ, consumer group |
| `outbound_sender_worker.py` | Reference: reclaim thread, DLQ schema |
| `medgemma_service/worker.py` | Reference: reclaim + mark_processed |
| `orchestrator_worker.py` | Signal consumer, conversation log read |
| `backend/models/events.py` | Event type definitions (`EscalationOpened`, `PhysicianNoteOpened`, …) |

# Changelog

## 2026-06-29 — Dermatologist Escalation Feature (Nova ↔ qutis-escalation)

Wires Nova to the **qutis-escalation dashboard** so low-confidence AI cases are handed to a human
dermatologist for review, and the physician's notes are delivered back to the end user. This is the
Nova-repo half; the dashboard half lives in the separate `qutis-escalation` repo. The two communicate
**only over HTTP**, authenticated with HMAC.

> Branch: `feat/escalation-integration`. Plans/runbook: `/Projects/escalation-cleanup-handoff/`
> (`06-NOVA-BACKEND-PLAN.md`, `05-NOVA-INTEGRATION-PLAN.md`, `RUNBOOK-joint-smoke-test.md`).
> Contract spec: `dermascan/ai_service/postprocessing/escalation.md`.

---

### 1. What this feature does (overview)

When MedGemma's diagnosis/product-review confidence falls below threshold (or the model output is
malformed), the case is **escalated**: relayed to a dashboard where a certified dermatologist reviews it
and writes notes. Those notes travel back into Nova and are composed into a user-facing reply that is
**authoritative over the AI's original answer**. The case image is viewable by the dermatologist only
while the case is open, and is revoked the moment the review is submitted.

Two trust zones, one shared secret (`QUTIS_INTERNAL_SECRET`):

```
┌─────────────────────────── NOVA (this repo) ───────────────────────────┐        ┌──────── qutis-escalation ────────┐
│                                                                         │        │  (separate repo, Next.js + PG)   │
│  MedGemma worker ──low-confidence──► escalation.py (producer)           │        │                                  │
│        (vision)                          │ XADD escalation:jobs         │        │                                  │
│                                          ▼                              │        │                                  │
│                              escalation_worker.py  (standalone process) │        │                                  │
│                                  ├─ enrich (conversation log)           │        │                                  │
│                                  ├─ cache signal_payload + image_path   │        │                                  │
│                                  └─ POST handoff ── HMAC ───────────────┼──────► │  /api/internal/escalations/ingest│
│                                                                         │        │     upsert + round-robin assign  │
│  FastAPI :8088                                                          │        │     (case appears in queue)      │
│   POST /v1/webhooks/escalation ◄── HMAC ── derma_notes ─────────────────┼──────  │  physician submits review        │
│        │ adds derma_notes to cached payload                             │        │                                  │
│        │ XADD orchestrator:signals  {mode}_physician_reviewed           │        │                                  │
│        │ SET escalation:closed:{case_id}                                │        │                                  │
│   GET /v1/escalations/{case_id}/image ◄── HMAC(path) ───────────────────┼──────  │  /api/cases/[id]/image (proxy)   │
│        └─ stream bytes, or 410 once closed                              │        │                                  │
│        ▼                                                                │        └──────────────────────────────────┘
│  orchestrator_worker ── composes reply led by derma_notes ──► outbound_sender ──► user (WhatsApp / Telegram)
└─────────────────────────────────────────────────────────────────────────┘
```

---

### 2. End-to-end data flow

**Forward (escalate):**
```
MedGemma inference
   → route_case_after_inference()                 [escalation.py]
       confidence < threshold?  →  XADD escalation:jobs  (+ always XADD orchestrator:signals {mode}_escalated, events:stream)
   → escalation_worker consumes escalation:jobs
       → enrich with last N conversation turns
       → cache escalation:signal_payload:{case_id}  + escalation:image_path:{case_id}
       → POST (HMAC) handoff packet  →  dashboard /api/internal/escalations/ingest
       → dashboard: upsert Case, round-robin assign, case shows in queue
```

**Return (physician notes → user):**
```
dashboard: physician submits review
   → POST (HMAC) {case_id, derma_notes}  →  Nova /v1/webhooks/escalation
       → load cached signal_payload, add ONLY derma_notes
       → XADD orchestrator:signals  {mode}_physician_reviewed
       → SET escalation:closed:{case_id}   (image now 410s)
   → orchestrator_worker consumes the signal
       → _compose_physician_reviewed_reply()  (derma_notes authoritative)
       → XADD outbound:messages
   → outbound_sender_worker delivers to the user's channel
```

**Image (view during review only):**
```
dashboard /api/cases/[id]/image
   → GET (HMAC over PATH)  →  Nova /v1/escalations/{case_id}/image
       → if escalation:closed:{case_id}      → 410 Gone
       → else download blob bytes by path    → 200 stream
```

---

### 3. Module-by-module changes

#### 3.1 Producer — `dermascan/ai_service/postprocessing/escalation.py` (modified, additive)
The triage stage that runs synchronously after MedGemma inference. Already existed; extended to carry the
data the downstream return-path needs.

- `_JobEntry` dataclass — **added** `image_blob_path: str = ""`.
- `route_case_after_inference(entry, text, confidence)` — unchanged logic: parses the model JSON, picks
  `mode` (`diagnose` / `product_review` / `hybrid`), compares confidence to the mode's threshold, and on
  escalation calls `_push_escalation`. Always emits a completion/escalation signal (`_push_signal`) and an
  audit event (`_push_event`).
- `_push_escalation(...)` — **added** these flat fields to the `escalation:jobs` `XADD`:
  `image_blob_path`, `review_type`, `rationale`, `summary` (JSON), `treatment` (JSON). This lets the worker
  reconstruct the full `{mode}_escalated` payload deterministically (see §3.3) without a racy stream join.
- `_stream_json(value)` — **new** helper: dict/list → `json.dumps`, scalars → string (Redis streams hold
  flat strings).
- **Guarantee:** the only changes here are additive fields — no existing field was renamed or reshaped.

#### 3.2 Image source plumbing — `dermascan/ai_service/modal/medgemma_service/worker.py` (modified)
- `_build_job_entry(...)` — **populates** `image_blob_path` on the job entry from the resolved image source
  (`image_source_value`); Firebase/GCS URLs are normalized to a durable blob path with
  `firebase_storage.extract_blob_path_from_url(...)`. This is the only place the image reference is in hand,
  so it is threaded forward here (not at the `route_case_after_inference` call site).
- Note: MedGemma has its own `_JobEntry` dataclass (duck-typed with the producer's); both carry the field.

#### 3.3 Escalation worker — `dermascan/backend/services/agents/escalation_worker.py` (new, ~470 lines)
A standalone Redis-stream consumer. **Not** started by `do_work.py` — run it as its own process
(`python -m dermascan.backend.services.agents.escalation_worker`), same as the knowledge worker.

- `EscalationJobPayload` (pydantic) — typed job model; includes `image_blob_path`, `summary`, `treatment`,
  `rationale`, `review_type`.
- `run_worker_forever()` — startup: hardened Redis client (`get_redis_client` from
  `shared/redis_stability.py` — **not** raw `Redis.from_url`), `reclaim_logic.set_redis_client`,
  `_ensure_consumer_group` (`XGROUP CREATE … mkstream`, ignore BUSYGROUP), a background reclaim thread, then
  the `XREADGROUP` loop dispatching to a `ThreadPoolExecutor`.
- `_process_escalation_job(msg_id, fields)` — the lifecycle:
  1. parse → emit `escalation_job_consumed`
  2. idempotency: `SET escalation:relayed:{case_id} NX EX 86400` — skip relay if already done (still ack)
  3. enrich: `LRANGE orchestrator:conversation_log:{client_id}` (last N, newest-first)
  4. `_build_escalated_signal_payload` + `_cache_signal_payload`
  5. `_relay_handoff` (HMAC POST, retries) → on success: emit `escalation_relay_completed`, `XACK`/`XDEL`/`mark_processed`
  6. on relay failure: **delete the relay idempotency key** (so re-escalation can retry), write `escalation:dlq`,
     emit `escalation_relay_failed`, enqueue `{mode}_escalation_deferred`, ack/del.
- `_build_escalated_signal_payload(job)` — builds the full `escalation.md` §7 payload **from job fields
  alone** (incl. `summary`/`treatment`/`rationale`). Deterministic — no `xrevrange` race against the
  orchestrator consuming the original signal.
- `_cache_signal_payload(job, payload)` — `SETEX escalation:signal_payload:{case_id}` (7d) with the
  **identity fields injected** (`request_id`/`case_id`/`client_id`/`message_id`/`channel`) so the return
  webhook can address the signal; also `SETEX escalation:image_path:{case_id}` when a blob path exists.
- `_build_handoff_packet(job, context)` — the `escalation.md` §8 packet (`case` + `conversation_context`,
  chronological). Carries **no** image reference — the dashboard fetches the image by `case_id` via the proxy.
- `_relay_handoff(handoff)` — signs the exact JSON body and POSTs to `ESCALATION_PLATFORM_URL` with
  exponential backoff; emits `escalation_relay_retry` per attempt.
- `_enqueue_escalation_deferred(payload, job)` — publishes `{mode}_escalation_deferred` so the user gets a
  holding message if the dashboard is unreachable (see §6 holding message).
- `_start_reclaim_thread` — periodic `reclaim_logic.reclaim_stale_pending` to recover stuck pending messages
  (DLQs after max reclaim attempts).

#### 3.4 FastAPI routes — `dermascan/backend/api/endpoints.py` (modified)
Two new HMAC-gated machine-to-machine routes on the existing `/v1` router (port 8088).

- `_verify_qutis_hmac(request)` — reads the **raw body** and verifies the signature (no re-serialization).
- `POST /v1/webhooks/escalation` → `escalation_physician_return_webhook`:
  verify HMAC → validate `{case_id, derma_notes}` (`physician_id`, `reviewed_at_ms`, `disposition`,
  `summary`, `clinical_notes` required; `follow_up` optional) → load `escalation:signal_payload:{case_id}`
  (404 if missing/expired) → `payload["derma_notes"] = derma_notes` (the **only** addition) →
  `XADD orchestrator:signals` `{mode}_physician_reviewed` → `SET escalation:closed:{case_id}` →
  emit `physician_review_completed` → delete the cached payload.
- `GET /v1/escalations/{case_id}/image` → `escalation_case_image_proxy`:
  verify HMAC **over the request path** (binds the signature to `case_id`) → 410 if
  `escalation:closed:{case_id}` exists → else read `escalation:image_path:{case_id}` (404 if missing) →
  `download_client_image_bytes_by_path` → stream with sniffed content-type.

#### 3.5 HMAC auth — `dermascan/shared/hmac_auth.py` (new)
Shared signing/verifying for Nova↔dashboard calls.

- `sign(body, ts)` = `HMAC-SHA256(QUTIS_INTERNAL_SECRET, f"{ts}.{body}")` hex.
- `signature_header(body, ts?)` → `("sha256=<hex>", "<unix_seconds>")`.
- `verify(raw_body, sig_header, ts_header)` — checks the `X-Qutis-Timestamp` is within a **300s** window,
  recomputes the signature, `hmac.compare_digest`. Raises `HTTPException(401)` on any failure (fail-closed,
  including when the secret is unset).
- `sign()` raises a plain `RuntimeError` if the secret is unset (it runs in the worker too); `verify()`
  translates that to 401.

#### 3.6 Firebase image-by-path — `dermascan/shared/firebase_storage.py` (modified)
- `download_client_image_bytes_by_path(blob_path)` — **new**: downloads bytes by durable blob path using the
  service account (no signed URL). Defense-in-depth: rejects any path not under the `client-media/` prefix.

#### 3.7 Orchestrator last-mile — `dermascan/backend/services/agents/orchestrator_worker.py` (modified)
This is where the physician's notes become the user's reply. **Critical:** the new branches are inserted
**before** the existing `signal_type.startswith("product_review_")` / `startswith("diagnose_")` routing —
otherwise a `diagnose_physician_reviewed` signal would fall into the normal rules flow, re-run the rules
engine, and silently discard `derma_notes`.

- `_process_signal_message(...)` — added at the top of the dispatch:
  - `signal_type.endswith("_physician_reviewed")` → `_compose_physician_reviewed_reply(payload)`
  - `signal_type.endswith("_escalation_deferred")` → `_ESCALATION_DEFERRED_MESSAGE`
  These branches do **not** call the rules engine or run research — the physician (or the deferral) is
  authoritative.
- `_compose_physician_reviewed_reply(payload)` — frames the reply by `disposition`
  (`confirmed` / `revised` / `unable_to_assess`), then appends `derma_notes.summary`, `clinical_notes`,
  `follow_up`, then a one-line AI footer (`Original AI finding: …`).
- `_ESCALATION_DEFERRED_MESSAGE` — fixed holding text used when the dashboard relay failed.

#### 3.8 Dashboard side (separate repo `qutis-escalation`) — for context
Mirrors the contract: `lib/server/hmac.ts` (same scheme), `app/api/internal/escalations/ingest` (verify +
idempotent upsert + round-robin), `lib/server/nova/relay-review.ts` (signs + POSTs `derma_notes` after the
DB commit, `relayedToNova` flag + retry drain), and `app/api/cases/[id]/image` proxying Nova bytes. The one
frozen seam that changed is `getCaseImageUrl` (returns a same-origin proxy URL). No `imageRef` column — the
image is keyed by `caseId` throughout.

---

### 4. Contracts

#### 4.1 HMAC (byte-identical on both repos)
```
signed_string = f"{unix_seconds}.{payload}"
signature     = hex(HMAC_SHA256(QUTIS_INTERNAL_SECRET, signed_string))
headers       = X-Qutis-Signature: sha256=<signature>   ·   X-Qutis-Timestamp: <unix_seconds>
replay window = 300 seconds   ·   compare = constant-time   ·   missing secret = fail closed (401)
POST: payload = exact raw request body bytes
GET (image): payload = the request PATH (/v1/escalations/{case_id}/image) — binds the signature to the case
```

#### 4.2 Redis keys
| Key | Written by | Purpose | Lifecycle |
|---|---|---|---|
| `escalation:jobs` (stream) | producer | human-review queue | consumed by worker |
| `escalation:dlq` (stream) | worker | poison / exhausted-relay jobs | manual replay |
| `escalation:relayed:{case_id}` | worker | relay idempotency | EX 24h; deleted on relay failure |
| `escalation:signal_payload:{case_id}` | worker | cached `{mode}_escalated` payload for the return path | EX 7d; deleted by webhook |
| `escalation:image_path:{case_id}` | worker | `case_id` → Firebase blob path | EX 7d |
| `escalation:closed:{case_id}` | return webhook | revoke flag → image 410s | EX 7d |
| `orchestrator:signals` (stream) | producer + webhook | `{mode}_escalated` / `_physician_reviewed` / `_escalation_deferred` | consumed by orchestrator |

#### 4.3 Environment variables (Nova)
| Var | Default | Notes |
|---|---|---|
| `QUTIS_INTERNAL_SECRET` | *(required)* | HMAC secret; **identical** to the dashboard's |
| `ESCALATION_PLATFORM_URL` | *(required)* | dashboard ingest URL the worker relays to |
| `ESCALATION_STREAM` | `escalation:jobs` | (renamed from the dead `ESCALATION_JOB_STREAM`) |
| `ESCALATION_DLQ_STREAM` | `escalation:dlq` | |
| `ESCALATION_CONSUMER_GROUP` / `_NAME` | `escalation-workers` / `escalation-worker` | |
| `ESCALATION_RELAY_MAX_ATTEMPTS` | `3` | |
| `ESCALATION_RELAY_BASE_DELAY_SEC` | `0.75` | |
| `ESCALATION_CONTEXT_MESSAGE_N` | `10` | falls back to `CONVERSATION_LOG_INCLUDE_N` |

Shared infra reused (not duplicated): `REDIS_URL`, `EVENTS_STREAM`, `ORCHESTRATOR_SIGNAL_STREAM`,
`WORKERS_*`, `CONVERSATION_LOG_*`.

---

### 5. Tests
- `dermascan/tests/unit/test_hmac_auth.py` — sign/verify round-trip, tampered body, stale timestamp, missing
  secret fail-closed, **GET path-binding** (a signature for case A is rejected for case B).
- `dermascan/tests/integration/test_escalation_full_loop.py` — producer emits `image_blob_path`; worker
  relays + caches with full fidelity (`summary`/`treatment` survive); return webhook publishes
  `{mode}_physician_reviewed` with `derma_notes`; image 410 after close; **cross-case replay → 401**;
  relay failure → `escalation:dlq` + `{mode}_escalation_deferred`.
- Helper: `dermascan/scripts/smoke_inject.py` injects one low-confidence case onto `escalation:jobs` for the
  joint smoke test (no MedGemma needed).

**Joint smoke test (2026-06-29): passed.** All three HMAC crossings (ingest, return, image), the
physician-led reply composition, and image revoke verified end-to-end across the TS↔Python boundary.

---

### 6. Known issue — outbound reply can be dropped when the channel sender is unreachable

**Severity:** medium · **Scope:** pre-existing `outbound_sender_worker` behavior, **not** the escalation
feature. Surfaced by the escalation smoke test.

**What happens.** The escalation loop ends correctly: the orchestrator composes the physician-led reply and
`XADD`s it to `outbound:messages`. `outbound_sender_worker` then delivers it to the user's channel — for
WhatsApp it POSTs to `WHATSAPP_API_BASE_URL` (e.g. `http://127.0.0.1:8000/send`, the Qutis WhatsApp bridge).
In the smoke test that bridge was not running:

```
outbound delivery failed: msg_id=…
ConnectionRefusedError: [Errno 61] Connection refused
HTTPConnectionPool(host='127.0.0.1', port=8000): Max retries exceeded with url: /send
```

**The problem.** After the send retries are exhausted, the message was found in **neither**
`outbound:messages` **nor** `outbound:dlq` — i.e. the composed reply was **silently dropped**, not queued
for retry and not preserved in a DLQ. So if the channel sender (WhatsApp bridge / Telegram API) is down, the
physician's reply to the patient is lost with no audit trail.

**Why it is not an escalation bug.** Escalation's responsibility ends when it publishes the
`{mode}_physician_reviewed` signal — which it did, with the correct `derma_notes`. The orchestrator composed
the right reply (verified). The loss is entirely at the final channel-delivery hop in the existing outbound
pipeline.

**Recommended fix (separate from this feature).** In `outbound_sender_worker.run_worker_forever`'s delivery
path, on exhausted send retries write the message to `outbound:dlq` (with `case_id`, `channel`, error,
`failed_at_ms`) before ack/del — matching the DLQ contract every other worker follows — and consider a
drain/retry for transient sender outages. Until then, **the WhatsApp bridge (`:8000`) must be running** for
escalation replies (and all outbound messages) to actually reach users.

---

### 7. Operational notes
- Run order for the full loop: Redis → Nova FastAPI (`:8088`) → escalation worker → dashboard (`:3000`) →
  (for delivery) `do_work.py` + the channel bridge.
- The escalation worker is standalone (like the knowledge worker / MedGemma) — add it to your process
  supervisor; it is intentionally **not** in `do_work.py`.
- `QUTIS_INTERNAL_SECRET` must be byte-identical in Nova `.env` and the dashboard `.env.local`, or every
  crossing returns 401.




1. Make "no products" a valid answer (root cause). Mirror the pattern you already use for reproductive status
  (reproductive_status_confirmed): add a confirmation flag to IntakeSession, e.g. products_confirmed_none. Concretely:

  - Add an optional field to update_intake (e.g. no_current_products: bool) that sets the flag when the user says they use nothing.
  - In _profile_field_is_gap / compute_profile_gaps (intake_session.py:228), treat current_products as satisfied when the flag is set
  even though the list is empty.
  - Add one line to the tool's field description so the model knows to use it ("set true when the user says they use no products").

  The alternative — changing the profile default from [] to None so an explicitly-stored empty list means "confirmed none" — touches
  serialization in Redis and Firestore and risks misreading existing profiles, so the session flag is the safer, smaller change.

  ⏺ Two distinct issues in that orchestrator log — one loud, one quiet. The loud one is harmless-ish; the
  quiet one is the real bug.

  1. The Firestore traceback: wrong client construction in delivery_context.py

  load_skin_profile_dict (dermascan/shared/delivery_context.py:143-148) does:

  initialize_firebase_app()
  from google.cloud import firestore
  firestore_client = firestore.Client()   # ← uses Application Default Credentials

  google.cloud.firestore.Client() ignores the firebase_admin app it just initialized and goes looking
  for ADC (gcloud auth application-default login), which you don't have set up — hence
  DefaultCredentialsError. Everywhere else in the codebase (e.g. _get_firestore_client in
  orchestrator_tools) the pattern is initialize_firebase_app() then from firebase_admin import
  firestore; firestore.client() — lowercase client(), which rides the service-account JSON. This one
  call site just uses the wrong import.

  It's caught and returns {}, so the flow continued — but it means the delivery/rules flow composed
  your result without the skin profile (no age/allergies context) whenever the Redis cache misses.
  One-line fix.

  2. The quiet one: your audit ran twice — duplicate jobs, duplicate replies

  Look at the correlation:

  - MedGemma gateway logged audit_image_ref endpoint called twice (20:50:49 and 20:50:55), but the
  orchestrator made only one tool call, and only one request got a 200 access log.
  - MedGemma worker then processed two jobs (f1bc6d… and c6ed3e…, batch dispatched: size=2) for the
  same image.
  - That produced two diagnose_completed signals, both workers ran the rules flow, and you got two
  near-identical proactive replies sent to WhatsApp (you can see both "Orchestrator proactive reply"
  blocks at 20:52:33 and 20:52:34, with slightly different wording).

  The cause is in _dispatch_audit_image_ref (orchestrator_tools.py:1564-1575): it retries on
  httpx.TimeoutException. The first POST reached the endpoint and enqueued a job, but the endpoint
  responded slower than MEDGEMMA_ENDPOINT_TIMEOUT_S, so the client timed out, retried, and enqueued a
  second job. The Redis dedupe key in the tool only guards against the tool being called twice — it
  doesn't help when one tool call fires two HTTP requests. The endpoint itself isn't idempotent: every
  call mints a new job_id.

  Fix options, in order of value:
  1. Make the endpoint idempotent per request_id — in audit_image_ref, SET NX a
  medgemma:enqueued:{request_id} key (or reuse the job payload's request_id as the dedupe) and return
  the existing job as already_queued on repeat. This kills the whole class regardless of who retries.
  2. Don't retry on read timeout in _dispatch_audit_image_ref — keep retries for ConnectError (request
  never arrived, safe) but treat TimeoutException after the request was sent as "assume delivered"
  rather than re-POST. Option 1 makes this less critical.
  3. Optionally also dedupe diagnose_completed signals per request_id on the orchestrator signal
  consumer, as a last line against duplicate outbound messages — same pattern as the per-request
  escalation lock you added earlier.
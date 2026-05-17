# Changelog — Nova (dermascan)

Maintained by Munene Wanjohi.

This document tracks Nova-side changes for the WhatsApp integration and records
open issues that the team still needs to align on before we implement fixes.
It is meant to be read by other developers — each section explains *what*
changed, *why*, and *what the effect is*.

---

## [Unreleased] — branch `feat/qutis-whatsapp-bridge`

### Context

Nova receives WhatsApp messages from the **qutis** gateway (a separate FastAPI
service that sits between 360dialog and Nova). The end-to-end path for an image
is:

```
WhatsApp user
  → 360dialog
  → qutis  (downloads the image, re-hosts it on DigitalOcean Spaces,
            replaces the media URL with a presigned Spaces URL)
  → Nova /v1/webhooks/whatsapp        (ingress, normalises the message)
  → orchestrator:jobs stream          (orchestrator agent / LLM)
  → diagnose:jobs stream              (diagnose worker → MedGemma)
  → outbound:messages stream          (outbound sender → qutis /send → user)
```

This branch makes that path work and adds full-pipeline logging so failures can
be located quickly.

---

### Changed files

#### `dermascan/backend/api/endpoints.py` — ingress

- **Meta CDN URLs are now blanked.** WhatsApp/Meta-hosted attachment URLs
  (`lookaside.fbsbx.com`, `fbcdn.net`) return `401` to anonymous server-side
  `GET`s, so they are useless to Nova. `_is_whatsapp_meta_cdn_url()` detects
  them and blanks the URL, forcing the message to fall back to a media-id
  attachment instead of a dead URL.
- **Ingress logging (`[NOVA-INGRESS]`).** Logs the raw payload received from
  qutis, the image normalisation decision (which `source.kind` was chosen and
  why), and the orchestrator job that gets enqueued.
- *Effect:* a bad/expired image URL no longer silently produces an
  unprocessable job, and you can see exactly what qutis delivered.

#### `dermascan/backend/services/agents/diagnose_worker.py` — diagnosis dispatch

- **New `whatsapp_media_id` resolution path.** When an attachment carries only a
  WhatsApp media id (no usable URL), the worker fetches the bytes from qutis via
  `GET {QUTIS_BASE_URL}/internal/whatsapp-media/{id}` using the shared internal
  secret.
- **Meta CDN URLs are skipped** in `_resolve_image` for the same `401` reason as
  above.
- **Diagnosis logging (`[DIAGNOSE-JOB]`, `[DIAGNOSE-RESOLVE]`,
  `[DIAGNOSE-MEDGEMMA]`).** Logs the full job payload received, the image
  resolution attempt for every attachment (URL fetch status, byte counts,
  failures), and the MedGemma dispatch + response.
- *Effect:* image-resolution failures are now explicit in the logs instead of a
  single ambiguous "missing image" warning.

#### `dermascan/backend/services/agents/orchestrator_worker.py` — orchestrator

- **WhatsApp reply threading.** The inbound `wamid` is stored in Redis
  (`orchestrator:wamid:{case_id}`, 2h TTL) when a job is picked up, so the
  diagnosis reply can be sent as a threaded reply to the original image even if
  the LLM passes a wrong `message_id` to a tool.
- **Hardened Redis connection** via `default_redis_connection_kwargs()`.
- **Job logging (`[ORCH-JOB]`).** Logs every orchestrator job pickup and the
  wamid stored for threading.

#### `dermascan/backend/services/agents/outbound_sender_worker.py` — delivery

- **WhatsApp threading policy.** Signal updates (diagnosis results) are always
  threaded to the original image; ordinary conversational replies are never
  threaded. Telegram behaviour is unchanged.
- **Typing indicator.** Before delivering a WhatsApp message the worker calls
  qutis `POST /typing` and waits ~1.5s so the "typing…" indicator appears just
  before the message.
- `_threading_decision` and `_deliver` were reworked to support the above.

#### `dermascan/backend/services/agents/memory/redis_checkpointer.py`

- Added `default_redis_connection_kwargs()` — TCP keepalive, socket timeouts,
  health-check interval, and exponential-backoff retry. Managed Redis providers
  close idle connections server-side; these settings keep the link healthy and
  recover transparently when a connection drops.

#### `dermascan/backend/services/tools/orchestrator/orchestrator_tools.py`

- **Hardened Redis connection** via `default_redis_connection_kwargs()`.
- **Enqueue logging (`[ENQUEUE-DIAGNOSE]`).** Logs the payload and attachment
  summary the orchestrator agent passes to the `diagnose:jobs` stream, and warns
  when the payload has no attachments.

#### `dermascan/shared/payload_log.py` — new

- Small shared helper module: `dumps_compact()` and `dumps_pretty()` for
  readable JSON in log lines.

---

### Environment variables

When running Nova workers against qutis, the following must be set **for every
worker process** (the diagnose worker and outbound sender worker are separate
processes and each load their own environment):

| Variable | Used by | Purpose |
|----------|---------|---------|
| `QUTIS_BASE_URL` | diagnose worker, outbound sender | Base URL of the qutis service (e.g. `http://127.0.0.1:8000`). |
| `QUTIS_INTERNAL_SECRET` | diagnose worker, outbound sender | Shared secret for qutis internal endpoints. **Must equal qutis's `INTERNAL_API_SECRET`.** |
| `QUTIS_MEDIA_HTTP_TIMEOUT_S` | diagnose worker | Timeout for fetching media from qutis. Optional, default `120`. |
| `LOG_LEVEL` | all | Log verbosity. Optional, default `INFO`. |

**Internal secret pairing:** qutis exposes internal endpoints (`/send`,
`/typing`, `/internal/whatsapp-media/...`) guarded by a header secret. qutis
reads that secret from `INTERNAL_API_SECRET`; Nova sends it as
`QUTIS_INTERNAL_SECRET`. **These two values must be identical** or every
internal call between the services fails with `401`.

---

## Known issues — open for team discussion

These are identified but **not yet fixed**. We need to align on the
implementation before changing anything.

### 1. Worker Redis connections are not hardened (HIGH)

`orchestrator_worker.py`, `orchestrator_tools.py`, and `redis_checkpointer.py`
use `default_redis_connection_kwargs()` (keepalive + health check + retry). The
**diagnose worker** and the **outbound sender worker** still create plain
connections:

```python
redis.Redis.from_url(REDIS_URL, decode_responses=True)   # no hardening
```

**Effect:** managed Redis closes idle TCP connections server-side. After an idle
period these two workers silently stop consuming their streams — no error, no
log output, the process is still alive. Observed live: a diagnose job sat
unconsumed for 24 minutes and no diagnosis or reply reached the user. It only
recovered after a manual restart.

**Fix direction:** apply `default_redis_connection_kwargs()` to the Redis
clients in both workers, the same way the orchestrator already does.

### 2. The orchestrator LLM corrupts the diagnosis image reference (HIGH)

The orchestrator agent enqueues a diagnose job by calling the
`enque_to_diagnose_stream` tool, passing the job `payload` as a **free-form
string argument**. That payload contains the presigned Spaces image URL, whose
object key embeds a long opaque WhatsApp message id (a hash-like base64 string).

The LLM hand-copies that string, and **intermittently mis-transcribes it**.
Observed: the message id segment `...QTlFNkFDMjcy...` became `...QTlFNkM2NDcy...`
between the orchestrator picking up the job and calling the tool. The URL is
byte-perfect through qutis → ingress → orchestrator job pickup, then corrupts at
the LLM tool call.

**Effect:** the corrupted URL points at a non-existent object and its signature
no longer matches the path, so DigitalOcean Spaces returns `403
SignatureDoesNotMatch`. The diagnose worker cannot fetch the image and reports
`diagnose_failed / image_unavailable` ("diagnose job missing image"). Because
LLM transcription is unreliable, this is **intermittent** — the same flow
succeeds whenever the LLM happens to copy the string correctly. This is the root
cause of images sometimes not getting a diagnosis. The LLM corrupts the
`message_id` argument the same way (currently masked by the
`orchestrator:wamid:{case_id}` threading fallback).

**Fix direction:** never route opaque/signed strings through an LLM tool
argument. The orchestrator already has the correct ingress payload server-side —
stash it in Redis (e.g. `orchestrator:job_payload:{request_id}`, the same
pattern already used for the wamid) and have `enque_to_diagnose_stream` use the
stashed `content.attachments` instead of the LLM-reconstructed URL.

### 3. Reply threading is fragile when the user keeps chatting (MEDIUM)

`orchestrator:wamid:{case_id}` is a single per-user slot, overwritten by every
inbound message.

**Effect:** if a user sends an image and then sends another message before the
diagnosis finishes, the slot is overwritten and the diagnosis reply threads to
the wrong message.

**Fix direction:** key the stored wamid per request (`request_id`) rather than
per case, or carry the image wamid on the diagnose job itself.

### 4. Typing indicator blocks the outbound sender worker (MEDIUM)

`_deliver` in the outbound sender worker calls qutis `POST /typing` and then
runs a blocking `time.sleep(1.5)` before sending each WhatsApp message. The
outbound sender is a single sequential consumer.

**Effect:** every WhatsApp reply is delayed ~1.5s, and up to ~6.5s if qutis
`/typing` is slow (5s HTTP timeout). The delay also holds up every message
queued behind it.

**Fix direction:** to discuss — reduce/remove the sleep, lower the HTTP timeout,
or fire the typing indicator earlier (when the job starts) instead of blocking
the delivery path.

### 5. Outbound backlog compaction after downtime (LOW / by design)

When the outbound sender worker restarts after downtime, the outbound stream
holds a backlog of stale replies. The worker's compaction logic collapses
multiple queued replies for the same user and delivers only the most relevant.

**Effect:** after an outage the user receives fewer messages than were
generated. This is acceptable behaviour for a stale backlog and is noted only so
it is not mistaken for message loss in normal operation.

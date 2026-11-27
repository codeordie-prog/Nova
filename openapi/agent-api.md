# Qutis Agent API (v0.1)

HTTP surface for **external agent clients** (OpenClaw plugin, MCP, future Actions). It does not replace Telegram or WhatsApp webhooks; it enqueues the same orchestrator pipeline with `channel=web` and lets clients **poll** for the assistant reply.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/agents/turns` | Enqueue one user turn |
| `GET` | `/v1/agents/turns/{request_id}` | Poll status and reply text |
| `GET` | `/health` | Liveness (unchanged) |

OpenAPI: [`openapi/qutis-agent.yaml`](../../openapi/qutis-agent.yaml) at the repository root.

## Authentication

Set `QUTIS_AGENT_API_KEY` on the API service. Clients send:

```http
Authorization: Bearer <QUTIS_AGENT_API_KEY>
```

When the env var is **unset**, auth is skipped (local development only).

## Request flow

1. `POST /v1/agents/turns` with `client_id`, optional `case_id`, and `text` and/or `image_base64`.
2. Response includes `request_id`, `case_id`, and `status: queued`.
3. Poll `GET /v1/agents/turns/{request_id}` until `status` is `done` (or `failed` / `unknown`).
4. The outbound sender stores `channel=web` replies in Redis for polling instead of sending Telegram/WhatsApp.

## Environment

| Variable | Default | Role |
|----------|---------|------|
| `QUTIS_AGENT_API_KEY` | (empty) | Bearer token for `/v1/agents/*` |
| `AGENT_TURN_TTL_S` | `86400` | TTL for ingress + reply Redis keys |
| `AGENT_API_JOB_SCAN_COUNT` | `500` | Job stream scan window for status |
| `AGENT_API_OUTBOUND_SCAN_COUNT` | `500` | Outbound stream scan window for status |

Workers and Redis must be running as for Telegram ingress (`do_work.py`, orchestrator, outbound sender).

Apply migration `dermascan/migrations/006_billing_agent_channels.sql` on your Postgres when billing is enabled, so `channel=web` accounts can be created.

## Client ids

Use a stable string per external session, for example `openclaw:<gatewaySessionId>`. Reuse `case_id` across turns in the same conversation when the client tracks it.

## Polling

Turn status is **`done` only when a final `job_reply` outbound is stored** for `channel=web`. Intermediate `signal_update` messages (common during product/rules-engine audits) keep status **`running`** so clients do not stop polling before the report arrives. Allow **up to ~10 minutes** for product reviews (`timeout_seconds` in Clawtis defaults to 600).

- Start with 1–2s interval; back off to 5–10s for long tool runs.
- **Clawtis:** `qutis_wait_for_turn` polls every `poll_interval_seconds`; `qutis_get_turn` is one-shot only (Qutis has no webhook to OpenClaw).
- `queued` → job not yet processed; `running` → job marked processed but no reply yet; `done` → `text` populated.
- Image uploads use the same validation and Firebase storage path as Telegram client images.

## Base URL

| Environment | URL |
|-------------|-----|
| **Hosted staging** | `https://dermascan-api-staging-plivf742uq-ew.a.run.app` |
| **Local** | `http://127.0.0.1:8000` |

Workers and Redis must be running on the deployment that backs that URL (Cloud Run workers for staging; `do_work.py` locally).

## Plugin repo

The OpenClaw plugin **Clawtis** lives in a **separate** MIT repo (`clawtis`, sibling to Nova) and should only implement HTTP against this contract—no Nova-specific coupling.
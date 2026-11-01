# Backend README

How to run the backend API, agent workers, and channel integrations.

## Prerequisites

- Python 3.12+
- Poetry
- Redis (`REDIS_URL`)
- PostgreSQL (required when product / skin rules-engine signal flows are active)
- Environment variables in `.env` (copy from `.env_example` at repo root)

```bash
poetry install
```

## API entrypoint

- Module: `dermascan/backend/app/main.py`
- Object: `app`
- Port: **8088**

```bash
poetry run uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port 8088 --reload
```

Health check: `GET /health`

Versioned routes are under `/v1`.

## Active endpoints

- `POST /v1/orchestrator/requests`
- `POST /v1/orchestrator/requests/batch`
- `GET /v1/orchestrator/requests/{request_id}`
- `POST /v1/media/test/upload`
- `POST /v1/webhooks/whatsapp`
- `POST /v1/webhooks/telegram`

Source: `dermascan/backend/api/endpoints.py`

## Orchestrator model: Qwen primary

The orchestrator worker (`orchestrator_worker.py`) builds a model chain in priority order:

1. **Qwen** (primary) — self-hosted on Modal via vLLM, OpenAI-compatible
2. **OpenAI** (`OPENAI_API_KEY_QUTIS`) — fallback
3. **Google Gemini** (`GEMINI_API_KEY_QUTIS`) — fallback

Qwen is enabled when `QWEN_BASE_URL` (or `QWEN_OPENAI_BASE_URL`) points at the deployed Modal endpoint. Deploy with:

```bash
poetry run python dermascan/scripts/deploy_qwen_app.py
```

Then set `QWEN_BASE_URL`, `QWEN_SERVED_MODEL_NAME`, and `QWEN_API_KEY` in `.env`.

## PostgreSQL & rules engine

Signal enrichment for product and skin reviews invokes `RulesEngine`, which connects to **PostgreSQL** to look up per-ingredient CIR safety data. These variables must be set before product-review flows will work:

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_DATABASE`
- `POSTGRES_USER`
- `POSTGRESS_PASSWORD` (spelling matches `rules_engine.py`)
- `POSTGRES_TABLE` (default `cir_pdfs`)

`REDIS_URL` is also required (ingredient index cache). `OPENAI_API_KEY_QUTIS` and `TAVILY_API_KEY_QUTIS` are needed for the ingredient-extraction and report subagents in `product_rules_signal_helper.py`.

## Start agent workers

Use the supervisor script (recommended):

```bash
poetry run python dermascan/scripts/do_work.py
```

This starts core workers in one process (plus optional lanes when flags are on):

| Worker | File | Stream(s) | When |
|---|---|---|---|
| Orchestrator | `orchestrator_worker.py` | `orchestrator:jobs`, `orchestrator:signals` | always |
| Outbound sender | `outbound_sender_worker.py` | `outbound:messages` | always |
| Escalation | `escalation_worker.py` | (escalation streams) | always |
| Billing | `billing_worker.py` | `billing:jobs` | `BILLING_ENABLED=true` |
| Reminders | `reminder_worker.py` | Postgres poll → outbound / signals | `REMINDERS_ENABLED=true` |

The **knowledge worker** (`knowledge_worker.py`) is present but intentionally excluded from `do_work.py`. Start it manually if needed:

```bash
poetry run python dermascan/backend/services/agents/knowledge_worker.py
```

**MedGemma** (gateway + worker) is not started by `do_work.py` — see the root `README.md`.

### What each worker does

**Orchestrator** — mouth and ears of the system. Consumes user jobs and internal signals, invokes Qwen (primary) with tool calling, enqueues outbound messages.

**Outbound sender** — compacts and delivers messages to Telegram or WhatsApp with ordered, deduplicated delivery.

**Reminders** — polls Postgres for due morning briefs and routine habits (WhatsApp v1). Respects the 24h customer window, MYDAWA pause, and local morning catch-up for pending briefs. Orchestrator tools: `set_morning_brief`, `set_routine_reminder`, `list_routines`, `disable_routine`.

### Reminders setup

1. Apply migration `dermascan/migrations/005_reminders.sql` on Postgres (same DB as billing).
2. Set `REMINDERS_ENABLED=true` and ensure `POSTGRES_*` match billing.
3. Restart `do_work.py` so the `reminders` thread starts.

See `.env_example` for `REMINDER_TICK_INTERVAL_S`, morning delivery window, and default timezone.

## Suggested local run order

1. Redis
2. PostgreSQL (rules-engine flows; also reminders and billing when enabled)
3. Deploy Qwen; set `QWEN_BASE_URL`
4. MedGemma gateway + worker (if image / skin review flows are required)
5. Backend API (`8088`)
6. `poetry run python dermascan/scripts/do_work.py`
7. (Optional) Telegram polling bridge for local testing:

```bash
poetry run python dermascan/backend/services/agents/telegram_polling_bridge.py
```

## Webhook payload contract (WhatsApp + Telegram)

Each accepted webhook message is normalized and pushed to `orchestrator:jobs` with:

- `request_id` — generated UUID
- `case_id` — conversation scope
- `client_id` — end-user identity
- `message_id` — platform message id
- `channel` — `"whatsapp"` or `"telegram"`
- `payload` — normalized JSON (`content.text`, `content.attachments[]`)

Telegram also includes `username` when available.

### WhatsApp (`POST /v1/webhooks/whatsapp`)

Expected message location: `entry[].changes[].value.messages[]` (or legacy `message`).

Minimum fields: `from` (client_id), `id` (message_id).

Supported content: `text.body`, image/document types, `attachments[]` with base64, URL, or `media_ref`/`blob_ref`.

- `case_id` = `client_id`
- If `WHATSAPP_INGRESS_SECRET` (or `INTERNAL_API_SECRET`) is set, send header `X-Internal-Secret`

### Telegram (`POST /v1/webhooks/telegram`)

Expected: `message` or `edited_message`.

Minimum fields: `chat.id` (case_id), `from.id` (client_id), `message_id`.

Supported: `text`, `caption`, `photo[]` (largest image downloaded and stored).

## Outbound WhatsApp delivery

`outbound_sender_worker.py` supports Telegram and WhatsApp. Configure for qutis sidecar:

- `WHATSAPP_API_BASE_URL` (default `http://127.0.0.1:8000`)
- `WHATSAPP_API_SEND_PATH` (default `/send`)
- `WHATSAPP_HTTP_TIMEOUT_S` (default `20`)
- `WHATSAPP_TLS_VERIFY` (default `true`)
- `WHATSAPP_CA_BUNDLE` (optional)
- `WHATSAPP_INTERNAL_API_SECRET` (falls back to `INTERNAL_API_SECRET`)

## Ingress security

Protect WhatsApp ingress with `WHATSAPP_INGRESS_SECRET` or `INTERNAL_API_SECRET`. The qutis sidecar should set `QUTIS_AI_INGRESS_SECRET` to the same value.

## MedGemma image source handling

Image analysis is dispatched directly to the MedGemma gateway from orchestrator tools using URL-based image references (`/audit_image_ref` or `/audit_ref`). The MedGemma worker resolves image bytes at inference time.

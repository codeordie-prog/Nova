# Dermascan Agents

AI agent network for dermatology: conversational orchestration, image diagnosis, product safety review, and multi-channel delivery (Telegram / WhatsApp).

## Architecture at a Glance

```
user (Telegram / WhatsApp)
         │
         ▼
  Backend API (FastAPI, :8088)
         │ XADD orchestrator:jobs
         ▼
  Orchestrator worker ──► Qwen (primary LLM, Modal vLLM)
         │                  └── fallbacks: OpenAI, Gemini
         ├──► MedGemma gateway (vision, via orchestrator tools)
         ├──► rules engine (PostgreSQL) via signal enrichment
         └──► outbound:messages ──► Telegram / WhatsApp
```

Workers are started together via `dermascan/scripts/do_work.py`. MedGemma runs as a separate AI-service stack (not included in that script).

## Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/)
- Redis (queues, job status, LangGraph checkpoints)
- PostgreSQL (rules engine ingredient safety data — required for product-review flows)
- Modal CLI (Qwen + MedGemma GPU inference)

## Setup

```bash
poetry install
cp .env_example .env   # fill in values — see sections below
```

## 1. Deploy Qwen (primary orchestrator LLM)

The orchestrator's primary model is **self-hosted Qwen** served on Modal via vLLM as an OpenAI-compatible HTTP endpoint. When `QWEN_BASE_URL` is set, the orchestrator worker uses Qwen first and falls back to OpenAI, then Gemini.

**What it does**

- Hosts `Qwen/Qwen3-8B` on an NVIDIA L40S GPU.
- Exposes `/v1/chat/completions` and `/v1/models` (standard OpenAI client).
- Supports tool calling (orchestrator tools: image audit, search, case status, etc.).

**Deploy**

```bash
# One-time Modal auth + Hugging Face secret
modal token new
modal secret create huggingface HF_TOKEN=

# Deploy (wrapper script)
poetry run python dermascan/scripts/deploy_qwen_app.py

# Or directly
modal deploy dermascan/ai_service/modal/qwen_service/app.py
```

After deploy, Modal prints a public HTTPS URL. Set in `.env`:

```bash
QWEN_BASE_URL=https://<your-modal-app>--dermascan-qwen-qwenservice-serve.modal.run/v1
QWEN_SERVED_MODEL_NAME=qwen-3-8b
QWEN_API_KEY=EMPTY
```

**Smoke-test the endpoint** (optional):

```bash
poetry run python dermascan/scripts/qwen_langchain_agent.py
```

**Shut down the deployed app** (stops GPU billing when you are done testing):

```bash
# List deployed Modal apps and copy the App ID for the Qwen deployment
modal app list

# Stop the app (replace with the App ID from the list)
modal app stop <app-id>
```

Look for an app named like `dermascan-qwen` in the `modal app list` output. Redeploy with the deploy command above when you need it again.

Config lives in `dermascan/ai_service/modal/qwen_service/config/config.json`.

## 2. PostgreSQL & rules engine

Product safety and skin-review signal enrichment call `RulesEngine` (`dermascan/rules_engine/engine/rules_engine.py`), which **queries PostgreSQL** for per-ingredient CIR safety data. Without a reachable Postgres instance and the variables below, product-review flows will fail at signal enrichment time.

Required variables (see `.env_example`):

| Variable | Purpose |
|---|---|
| `POSTGRES_HOST` | Database host |
| `POSTGRES_PORT` | Port (default `5432`) |
| `POSTGRES_DATABASE` | Database name |
| `POSTGRES_USER` | Username |
| `POSTGRESS_PASSWORD` | Password (note: double-S spelling matches code) |
| `POSTGRES_TABLE` | Ingredient table (default `cir_pdfs`) |

`REDIS_URL` is also required by the rules engine for ingredient-index caching.

## 3. Run the backend API

- Module: `dermascan/backend/app/main.py`
- Port: **8088**

```bash
poetry run uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port 8088 --reload
```

Active routes (prefix `/v1`):

- `GET /health`
- `POST /v1/orchestrator/requests`
- `POST /v1/orchestrator/requests/batch`
- `GET /v1/orchestrator/requests/{request_id}`
- `POST /v1/media/test/upload`
- `POST /v1/webhooks/whatsapp`
- `POST /v1/webhooks/telegram`

See `dermascan/backend/README.md` for webhook payload contracts and operational detail.

## 4. Run agent workers

Start orchestrator and outbound-sender workers in one process:

```bash
poetry run python dermascan/scripts/do_work.py
```

This script runs:

| Worker | Role |
|---|---|
| `orchestrator_worker` | Consumes jobs + signals; drives Qwen (primary) conversation |
| `outbound_sender_worker` | Compacts and delivers replies to Telegram / WhatsApp |

**Not included:** `knowledge_worker` (optional, start manually) and **MedGemma** (separate AI-service stack below).

### Suggested local run order

1. Redis
2. PostgreSQL (if using product / rules flows)
3. Deploy Qwen on Modal; set `QWEN_BASE_URL` in `.env`
4. MedGemma gateway + worker (if image / skin review flows are needed)
5. Backend API (`8088`)
6. `poetry run python dermascan/scripts/do_work.py`
7. (Optional) `telegram_polling_bridge.py` for local Telegram testing

## 5. MedGemma (vision diagnosis — separate)

MedGemma is **not** started by `do_work.py`. It runs as its own async stack:

- **Gateway (FastAPI / uvicorn):** validates requests, enqueues to Redis
- **Worker:** consumes `medgemma:jobs`, calls Modal GPU inference
- **Modal app:** hosts `MedGemmaService` GPU methods

```bash
# Gateway
poetry run python -m uvicorn dermascan.ai_service.modal.medgemma_service.gateways.endpoints:web_app --host 0.0.0.0 --port 8080 --reload

# Worker
poetry run python dermascan/ai_service/modal/medgemma_service/worker.py
```

Set `MEDGEMMA_GATEWAY_URL=http://127.0.0.1:8080` in `.env` so orchestrator tools can reach the gateway.

Modal setup:

```bash
modal secret create huggingface HF_TOKEN=
modal secret create redis REDIS_HOST= REDIS_PORT= REDIS_USERNAME= REDIS_PASSWORD= REDIS_DB=
```

See `dermascan/ai_service/modal/medgemma_service/README.md` for gateway config.

## Project structure

| Directory | Purpose |
|---|---|
| `dermascan/backend` | API server, agent workers, orchestrator tools |
| `dermascan/ai_service` | MedGemma + Qwen Modal apps, inference postprocessing |
| `dermascan/rules_engine` | PostgreSQL-backed ingredient safety engine |
| `dermascan/scripts` | Worker supervisor (`do_work.py`), Qwen deploy/test scripts |
| `dermascan/shared` | Firebase storage, logging utilities |
| `dermascan/frontend` | Web UI and client-side application |

## Further reading

- `dermascan/Agents.md` — full agent map, signal flows, safety pipeline
- `dermascan/backend/README.md` — API, webhooks, worker details
- `dermascan/ai_service/docs/inference.md` — inference pipeline overview

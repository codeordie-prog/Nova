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
  Orchestrator worker ──► OpenAI GPT-4o (primary LLM, tool calling)
         │                  └── fallback: Gemini 3.5 Flash
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
- Modal CLI (MedGemma GPU inference only)
- OpenAI API key (`OPENAI_API_KEY_QUTIS`) and Gemini credentials (`GEMINI_API_KEY_QUTIS` or Vertex AI)

## Setup

```bash
poetry install
cp .env_example .env   # fill in values — see sections below
```

## 1. Orchestrator LLM (OpenAI → Gemini)

The orchestrator uses **OpenAI GPT-4o** as the primary model (including tool calling) and **Gemini 3.5 Flash** as the fallback when OpenAI is rate-limited, unavailable, or its circuit breaker is open.

Skin and product **report subagents** (`product_rules_signal_helper`) use the opposite chain: **Gemini primary** via `REPORT_AGENT_MODEL`, OpenAI fallback.

Required variables (see `.env_example`):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY_QUTIS` | OpenAI API key for orchestrator primary |
| `ORCHESTRATOR_OPENAI_MODEL` | Primary model (default `gpt-4o`) |
| `ORCHESTRATOR_GEMINI_MODEL` | Orchestrator fallback (default `gemini-3.5-flash`) |
| `REPORT_AGENT_MODEL` | Gemini model for skin/product report subagents (default `gemini-3.5-flash`) |
| `ORCHESTRATOR_GEMINI_THINKING_LEVEL` | Gemini reasoning depth for fallback/report paths |
| `GOOGLE_APPLICATION_CREDENTIALS_QUTIS` | Service account JSON for Vertex AI |
| `GOOGLE_CLOUD_PROJECT_QUTIS` | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | Vertex region (use `global` for Gemini 3.x) |

Circuit-breaker tuning: `OPENAI_CB_*` and `GEMINI_CB_*` env vars control failover thresholds and cooldowns.

Model defaults and temperature/token limits live in `dermascan/backend/services/agents/configurations/agents_config.json`.

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
| `orchestrator_worker` | Consumes jobs + signals; drives OpenAI → Gemini conversation |
| `outbound_sender_worker` | Compacts and delivers replies to Telegram / WhatsApp |

**Not included:** `knowledge_worker` (optional, start manually) and **MedGemma** (separate AI-service stack below).

### Suggested local run order

1. Redis
2. PostgreSQL (if using product / rules flows)
3. Set `OPENAI_API_KEY_QUTIS` and Vertex/Gemini credentials in `.env`
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
| `dermascan/ai_service` | MedGemma Modal app, inference postprocessing |
| `dermascan/rules_engine` | PostgreSQL-backed ingredient safety engine |
| `dermascan/scripts` | Worker supervisor (`do_work.py`) and utility scripts |
| `dermascan/shared` | Firebase storage, logging utilities |
| `dermascan/frontend` | Web UI and client-side application |

## Further reading

- `dermascan/Agents.md` — full agent map, signal flows, safety pipeline
- `dermascan/backend/README.md` — API, webhooks, worker details
- `dermascan/ai_service/docs/inference.md` — inference pipeline overview

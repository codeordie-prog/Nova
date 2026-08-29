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
  Orchestrator worker ──► model chain (see §1)
         │                  OpenAI: gpt-4o → gpt-4o-mini → gpt-5-mini → gpt-5
         │                  then Gemini: gemini-3.5-flash → gemini-3.1-flash-lite
         │                  per-model circuit breakers + full-chain retries
         ├──► MedGemma gateway (vision, via orchestrator tools)
         ├──► rules engine (PostgreSQL) via signal enrichment
         │         report subagents: Gemini-first chain (REPORT_MODEL_CHAIN)
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

### Local infrastructure

Rather than pointing at Redis Cloud and Cloud SQL, run both locally:

```bash
./dermascan/scripts/local_stack.sh up
```

This starts Redis and Postgres (Docker Compose, falling back to Homebrew
services), creates `qutis_db`, applies the billing migrations, and writes a
`.env.local` overlay that redirects `REDIS_URL` and `POSTGRES_*` at the local
stack while your `.env` keeps supplying API keys. Delete `.env.local` to go back
to the cloud. See `dermascan/docs/local-stack.md`.

## 1. LLM model chains

The orchestrator and report subagents no longer use a single primary/fallback pair. Each flow runs an **ordered model chain** implemented in `dermascan/backend/services/agents/model_fallback_chain.py`.

On each model call the worker walks the chain left to right. If a model returns 429, times out, hits a length limit, or its **per-model circuit breaker** is open, the next entry is tried. When every model in the chain fails, the worker waits (`MODEL_CHAIN_RETRY_DELAY_S`) and re-walks the full chain up to `MODEL_CHAIN_MAX_RETRIES` times.

### Orchestrator (Gemini first, then OpenAI)

Used for conversational tool calling in `orchestrator_worker.py`:

```
gemini:gemini-3.5-flash → gemini:gemini-3.1-flash-lite
    → openai:gpt-4o → openai:gpt-5
```

Configure with `ORCHESTRATOR_MODEL_CHAIN` (comma-separated `provider:model_id` entries).

Additional orchestrator resilience:

- **Recursion retries** — on `GraphRecursionError`, reset the client checkpoint and retry the turn (`ORCHESTRATOR_RECURSION_MAX_RETRIES`).
- **Tool-call caps** — limit polling tools such as `get_time` and `read_status_of_a_case`.
- **Summarization** — checkpoint compression uses `SUMMARIZATION_MODEL_CHAIN` (Gemini-first by default).

### Report subagents (Gemini first, then OpenAI)

Skin/product reports and ingredient extraction in `product_rules_signal_helper.py` use a **cost-optimized** chain:

```
gemini:gemini-3.5-flash → gemini:gemini-3.1-flash-lite
    → openai:gpt-4o-mini → openai:gpt-4o
```

Configure with `REPORT_MODEL_CHAIN`. Subagents also have per-tool limits and invoke retries (`REPORT_AGENT_*`).

### Reasoning models (gpt-5 / o-series)

OpenAI reasoning models consume output tokens for hidden reasoning. Set a higher budget so tool calls still fit:

| Variable | Purpose |
|---|---|
| `OPENAI_REASONING_MAX_TOKENS` | Max completion tokens for gpt-5 / o-series (default `8192`) |
| `OPENAI_REASONING_EFFORT` | Reasoning depth (default `minimal`) |

### Required credentials & tuning

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY_QUTIS` | OpenAI API key |
| `GEMINI_API_KEY_QUTIS` or Vertex | Gemini / Google AI |
| `GOOGLE_APPLICATION_CREDENTIALS_QUTIS` | Service account JSON for Vertex AI |
| `GOOGLE_CLOUD_PROJECT_QUTIS` | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | Vertex region (use `global` for Gemini 3.x) |
| `ORCHESTRATOR_GEMINI_THINKING_LEVEL` | Gemini thinking depth on non-lite models |
| `ORCHESTRATOR_MAX_TOKENS` | Default max output tokens per call (non-reasoning OpenAI) |
| `MODEL_CB_FAILURE_THRESHOLD` / `MODEL_CB_COOLDOWN_S` | Per-model circuit breaker |
| `MODEL_CHAIN_MAX_RETRIES` / `MODEL_CHAIN_RETRY_DELAY_S` | Full-chain retry when all models fail |

Legacy `ORCHESTRATOR_OPENAI_MODEL`, `ORCHESTRATOR_GEMINI_MODEL`, and `REPORT_AGENT_MODEL` are ignored when the corresponding `*_MODEL_CHAIN` env vars are set.

Model temperature and token defaults also live in `dermascan/backend/services/agents/configurations/agents_config.json`.

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
| `orchestrator_worker` | Consumes jobs + signals; walks `ORCHESTRATOR_MODEL_CHAIN` with per-model breakers |
| `outbound_sender_worker` | Compacts and delivers replies to Telegram / WhatsApp |

**Not included:** `knowledge_worker` (optional, start manually) and **MedGemma** (separate AI-service stack below).

### Suggested local run order

1. `./dermascan/scripts/local_stack.sh up` — Redis, Postgres and migrations
2. Set `OPENAI_API_KEY_QUTIS` and Vertex/Gemini credentials in `.env`
3. MedGemma gateway + worker (if image / skin review flows are needed)
4. Backend API (`8088`)
5. `poetry run python dermascan/scripts/do_work.py`
6. (Optional) `telegram_polling_bridge.py` for local Telegram testing

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
| `dermascan/backend/services/agents` | Workers, `model_fallback_chain.py`, circuit breakers |
| `dermascan/ai_service` | MedGemma Modal app, inference postprocessing |
| `dermascan/rules_engine` | PostgreSQL-backed ingredient safety engine |
| `dermascan/scripts` | Worker supervisor (`do_work.py`) and utility scripts |
| `dermascan/shared` | Firebase storage, logging utilities |
| `dermascan/frontend` | Web UI and client-side application |

## Further reading

- `dermascan/Agents.md` — full agent map, signal flows, safety pipeline
- `dermascan/backend/README.md` — API, webhooks, worker details
- `dermascan/ai_service/docs/inference.md` — inference pipeline overview

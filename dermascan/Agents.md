# Dermascan – Agents Map

> A guide to every agent in the system: what it does, where it lives, and how it
> participates in the larger flow.
>
> Companion documents in `dermascan/`:
> - `claude.md` — reliability and scale review
> - `review.md` — production readiness review
> - `backend/services/agents/awareness.md` — orchestrator’s self-description
> - `backend/services/agents/soul.md` — system identity / clinical principles

---

## 1. High-level Picture

```
                user (Telegram / WhatsApp)
                          │
                          ▼
           ┌──────────────────────────────┐
           │   Backend ingress (FastAPI)  │
           │  backend/api/endpoints.py    │
           │         port 8088            │
           └──────────────┬───────────────┘
                          │ XADD orchestrator:jobs
                          ▼
        ┌────────────────────────────────────────┐
        │           Orchestrator Agent           │
        │  backend/services/agents/              │
        │      orchestrator_worker.py            │
        │                                        │
        │   LLM: primary or alternate chain      │
        │   (model_chain_routing +               │
        │    model_fallback_chain)               │
        │   Gemini-first OR OpenAI-first         │
        │                                        │
        │   reads: orchestrator:jobs             │
        │          orchestrator:signals  <───────┼── awareness loop
        │   writes: outbound:messages            │
        └───────────┬────────────────┬───────────┘
                    │                │
        direct MedGemma dispatch     │ rules flows + signals
                    ▼                │
        ┌──────────────────┐         │
        │ MedGemma Service │         │
        │ ai_service/modal │         │
        │   /medgemma_*    │         │
        └────────┬─────────┘         │
                 │ result + signal   │
                 ▼                   ▼
        ┌──────────────────────────────────────────┐
        │   Signal helpers (rules + research)      │
        │   product_rules_signal_helper.py         │
        │   - Ingredient subagent                  │
        │   - Skin-review subagent                 │
        │   - User-friendly report subagent        │
        │   - RulesEngine ──► PostgreSQL (CIR)     │
        └────────┬─────────────────────────────────┘
                 │ enriched signal (XADD)
                 ▼
        ┌──────────────────────────────────────┐
        │       Outbound Sender Agent          │
        │  outbound_sender_worker.py           │
        │  (compaction + ordered delivery)     │
        └────────┬─────────────────────────────┘
                 ▼
              user (Telegram / WhatsApp)
```

### Starting workers locally

Backend agent workers are started together via:

```bash
poetry run python dermascan/scripts/do_work.py
```

This runs orchestrator and outbound-sender in one supervisor process. **MedGemma** (gateway + worker) and the **knowledge worker** are started separately — see root `README.md`.

---

## 2. The Agents

### 2.1 Orchestrator Agent
- **File:** `dermascan/backend/services/agents/orchestrator_worker.py`
- **Prompt:** `dermascan/backend/services/agents/prompts/orchestrator_prompt.py`
- **Personality / principles:** `dermascan/backend/services/agents/soul.md`
- **Self-description:** `dermascan/backend/services/agents/awareness.md`
- **LLM providers:** Gemini (Vertex AI or AI Studio) and OpenAI (`OPENAI_API_KEY_QUTIS`), wired through ordered model chains — not a single primary/fallback pair.

**Role**
- It is the **mouth and ears** of the system.
- It consumes user messages from `orchestrator:jobs` and produces user-facing replies.
- It also consumes internal events from `orchestrator:signals` and proactively reaches out to the user when something important has happened (a completed diagnosis, a high-confidence signal, a poison-pill case, etc.).

**Model chains (two layers)**

1. **Which playlist?** — `model_chain_routing.py` (Layer 0)  
   At the start of each turn (`request_id`), the worker binds **primary** (Gemini-first) or **alternate** (OpenAI-first):
   - Normal: primary unless Redis key `model_chain:gemini_degraded` is set (after enough Gemini health errors fleet-wide).
   - Test: set `MODEL_CHAIN_PREFER_PRIMARY=false` to always use the alternate chain.
   - The choice is **sticky for the whole turn** (orchestrator steps + checkpoint summarization).

2. **Walk the playlist** — `model_fallback_chain.py` (Layer 1)  
   On each LLM step, models are tried **left to right**. Transient failures (timeout, 504, **499/cancelled**, rate limits, etc.) fall through to the next model. Each model has a **sliding-window circuit breaker** (`provider_circuit_breaker.py`). If every model fails, the worker may **re-walk the same playlist** after `MODEL_CHAIN_RETRY_DELAY_S` (`MODEL_CHAIN_MAX_RETRIES`).

**Default chain specs** (override via env):

| Mode | Orchestrator (`ORCHESTRATOR_MODEL_CHAIN`) | Summarization (`SUMMARIZATION_MODEL_CHAIN`) |
|------|---------------------------------------------|---------------------------------------------|
| Primary | `gemini-3.5-flash` → `gemini-3.1-flash-lite` → `gpt-4o` → `gpt-5` | `gemini-3.1-flash-lite` → `gpt-4o-mini` |
| Alternate | `gpt-4o-mini` → `gpt-5-mini` → `gemini-3.1-flash-lite` → `gemini-3.5-flash` | `gpt-4o-mini` → `gpt-5-mini` → `gemini-3.1-flash-lite` |

Alternate orchestrator/summarization lists: `ORCHESTRATOR_MODEL_CHAIN_ALT`, `SUMMARIZATION_MODEL_CHAIN_ALT`. Degraded-mode tuning: `MODEL_CHAIN_GEMINI_ERROR_THRESHOLD`, `MODEL_CHAIN_GEMINI_ERROR_WINDOW_S`, `MODEL_CHAIN_GEMINI_DEGRADED_TTL_S`, `MODEL_CHAIN_TURN_STICKY_TTL_S`.

Gemini failures on a step also call `notify_gemini_model_failure` (via `ModelChainRunner.on_model_failure`) to increment the Redis error counter toward degraded mode.

**Two-loop design**
1. **Job loop** — reactive: user sends something, orchestrator replies.
2. **Signal loop (awareness)** — proactive: an internal event triggers a message to the user *without* a user prompt.

This dual-loop design is what `awareness.md` describes as its **internal forever loop on the signal stream**.

---

### 2.2 Qwen Service (optional self-hosted LLM)
- **Folder:** `dermascan/ai_service/modal/qwen_service/`
- **Modal app:** `app.py` (`QwenService` class, GPU L40S)
- **Deploy script:** `dermascan/scripts/deploy_qwen_app.py`

**Role**
- Optional: serves `Qwen/Qwen3-8B` via vLLM as an OpenAI-compatible HTTP API.
- **Not** the default orchestrator path today — production orchestrator uses `ORCHESTRATOR_MODEL_CHAIN` (Gemini/OpenAI via `model_fallback_chain.py`). Qwen remains available for experiments or a custom chain entry if you add `openai:` pointed at `QWEN_BASE_URL` in the chain spec.

---

### 2.3 MedGemma Service (Vision Diagnosis)
- **Folder:** `dermascan/ai_service/modal/medgemma_service/`
- **Gateway (FastAPI):** `gateways/endpoints.py`
- **Worker (Redis stream consumer):** `worker.py`
- **Model app (Modal class):** `app.py`
- **Queue/rate limit:** `gateways/queue_depth_manager.py`, `gateways/global_rate_limiter.py`

**Role**
- Receives image + prompt at the gateway.
- Enqueues the request into `medgemma:jobs` under capacity gating.
- The worker reads the stream in **micro-batches**, dispatches to the Modal function (`run_batch.spawn`), and writes per-job results to Redis.
- A **confidence score** is computed from token logprobs — see `confidence_score_algorithm.md`.

This is the system's **eye and clinical-judgment substrate**.

---

### 2.4 Outbound Sender Agent
- **File:** `dermascan/backend/services/agents/outbound_sender_worker.py`

**Role**
- Reads `outbound:messages` and delivers to the channel (currently Telegram).
- Performs **compaction**: deduplicate, suppress redundant progress messages, prefer one progress reply per turn, drop a progress reply when a signal already covered that turn.
- Performs **ordered delivery** keyed by `(inbound_ts, turn, kind, created_at, seq)`.
- Detects **chaos mode** (bursty multi-turn input) and switches Telegram threading behavior accordingly.

This agent is the **mouth-control layer** — it ensures the user sees a coherent conversation even when multiple flows produce content concurrently.

---

### 2.5 Knowledge Agent
- **File:** `dermascan/backend/services/agents/knowledge_worker.py`
- **Status:** present in the repo, intentionally **not started by `dermascan/scripts/do_work.py`**.

**Role (when active)**
- Consumes `knowledge:jobs`.
- Performs research/enrichment (with retries + degraded-mode fallback).
- Emits a `knowledge_completed` **signal** so the orchestrator can deliver the enriched answer proactively.

---

### 2.6 Subagents in `product_rules_signal_helper.py`
- **File:** `dermascan/backend/services/agents/product_rules_signal_helper.py`

This module hosts **three small specialized agents** plus two flow functions used during signal enrichment.

#### a) Ingredient Extraction Subagent
- **Builder:** `_build_ingredient_agent()`
- **Tool:** `inci_decoder_search` (Tavily, restricted to `incidecoder.com`)
- **Output schema:** `IngredientExtractionResponse(ingredients, notes, sources)`
- **Purpose:** Given a product name + description, return a clean **INCI-style** ingredient list (no percentages, deduped, normalized via `_normalize_inci_name`).

#### b) User-Friendly Report Subagent
- **Builder:** `_build_report_agent()`
- **Tool:** none
- **Output schema:** `UserFriendlyReportResponse(report_text)`
- **Purpose:** Convert the rules-engine output into a concise, non-technical safety report suitable for sending to the user.

#### c) Skin-Review Research Subagent
- **Builder:** `_build_skin_review_agent()`
- **Tool:** `skin_review_search` (Tavily, general medical sources)
- **Output schema:** `SkinReviewResponse(report_text)`
- **Purpose:** Given a diagnosis, summary, and treatment, generate an evidence-based, plain-language educational note (with disclaimers, no prescriptions).

#### d) Flow functions (orchestrate the subagents)
- `run_product_signal_rules_flow(fields)`:
  1. Extract ingredients via the **ingredient subagent**.
  2. Build a `RulesEngineRequest` with `review_type="product"`.
  3. Run `RulesEngine.process(...)` — see `dermascan/rules_engine/`.
  4. Render with the **report subagent**.
- `run_skin_signal_rules_flow(fields)`:
  1. Build a skin-mode `RulesEngineRequest`.
  2. Run the rules engine.
  3. Render with the **skin-review subagent**.

These flows are invoked by the orchestrator’s **signal loop** when the corresponding `signal_type` arrives.

---

## 3. Where Each Agent Lives (Quick Index)

| Agent / Service | File |
|---|---|
| Worker supervisor | `dermascan/scripts/do_work.py` |
| Orchestrator | `dermascan/backend/services/agents/orchestrator_worker.py` |
| Outbound Sender | `dermascan/backend/services/agents/outbound_sender_worker.py` |
| Knowledge | `dermascan/backend/services/agents/knowledge_worker.py` |
| Qwen Modal app | `dermascan/ai_service/modal/qwen_service/app.py` |
| Qwen deploy script | `dermascan/scripts/deploy_qwen_app.py` |
| MedGemma gateway | `dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py` |
| MedGemma worker | `dermascan/ai_service/modal/medgemma_service/worker.py` |
| MedGemma model app | `dermascan/ai_service/modal/medgemma_service/app.py` |
| Rules engine | `dermascan/rules_engine/engine/rules_engine.py` |
| Ingredient subagent | `dermascan/backend/services/agents/product_rules_signal_helper.py` (`_build_ingredient_agent`) |
| Report subagent | same file (`_build_report_agent`) |
| Skin-review subagent | same file (`_build_skin_review_agent`) |
| Product signal flow | same file (`run_product_signal_rules_flow`) |
| Skin signal flow | same file (`run_skin_signal_rules_flow`) |
| Group report aggregator | `dermascan/backend/services/agents/group_report_aggregator.py` |
| Telegram polling bridge | `dermascan/backend/services/agents/telegram_polling_bridge.py` |
| Model chain (Layer 1 fallback) | `dermascan/backend/services/agents/model_fallback_chain.py` |
| Model chain routing (Layer 0) | `dermascan/backend/services/agents/model_chain_routing.py` |
| Per-model circuit breakers | `dermascan/backend/services/agents/provider_circuit_breaker.py` |
| Agent build / Gemini clients | `dermascan/backend/services/agents/build.py` |
| Config loader | `dermascan/backend/services/agents/loader.py` |

---

## 4. How `awareness.md` Works in Practice

`awareness.md` describes the orchestrator’s **internal awareness loop** — its ability to act *without* a user prompt. Concretely:

### 4.1 The signal stream
- Stream: `orchestrator:signals`
- Consumer group: `orchestrator-signal-workers`
- Reader: `_reader_loop(...)` running in the **signal thread** inside `orchestrator_worker.run_worker_forever()`.

### 4.2 When a signal is produced
Per `awareness.md` and the codebase, signals are emitted in two main situations:

1. **Successful diagnosis above the confidence threshold.**
   The MedGemma worker’s post-inference routing (see `ai_service/postprocessing/escalation.py` → `route_case_after_inference`) writes a signal so the orchestrator can proactively share the diagnosis.

2. **Failed / poisoned cases.**
   When retries are exhausted or a job is DLQ’d, a signal is emitted so the orchestrator can tell the user: *“we couldn’t complete this — please try again.”*

Other signal sources include enrichment completion (knowledge worker, product rules flow, skin rules flow).

### 4.3 What the orchestrator does with a signal
Inside `_process_signal_message(...)`:
1. Parses the signal payload.
2. Optionally invokes a **rules-flow** (product or skin) via `product_rules_signal_helper.py`.
3. Builds a structured prompt for itself with `_build_signal_input(...)` (so the LLM knows this is a proactive flow, not a user turn).
4. Invokes the agent against the same per-client checkpoint namespace.
5. Strips markdown, appends a **proactive log entry** (`orchestrator:proactive_log:<client_id>`), and enqueues an outbound message of `kind="signal_update"`.
6. Outbound sender then compacts and delivers.

### 4.4 Why this matters
This is what makes the system **transcend the “prompt → response” pattern**. As `awareness.md` puts it, the orchestrator is *“autonomous in a degree that it autonomically consumes entry jobs … but also can be triggered by the signal stream to proactively reach out to the client.”*

It is the system’s **clinical reflex arc**: it does not need to be asked.

---

## 5. Role of `soul.md`

`soul.md` is the **identity and ethics layer** of the orchestrator. It is *not* code — it is the permanent character the orchestrator embodies whenever it speaks to the user. Concretely:

- **Identity:** I am Qutis, a dermatology-focused clinical intelligence by QutisAI.
- **Mission:** improve care quality, clinical velocity, and patient understanding while respecting human oversight.
- **Clinical boundaries:** never overconfident, always state uncertainty, escalate when risk is non-trivial.
- **Communication style:** concise, calm, empathetic, plain language by default.
- **Status discipline:** never repeat already-delivered updates verbatim — communicate **deltas only**.
- **Operational discipline:** use tools when they materially help, never invent state or diagnoses.

`soul.md` is loaded into the orchestrator’s system prompt (see `prompts/orchestrator_prompt.py`) so every response is shaped by it. It is what gives the system a **consistent voice** across both reactive (job) and proactive (signal) flows.

If `awareness.md` describes **how** the orchestrator perceives, `soul.md` describes **who is doing the perceiving**.

---

## 6. How We Acknowledge Safety (Ingredient-by-Ingredient)

Safety review for cosmetic products is not done by an LLM guess — it is grounded in a **deterministic rules engine over individual ingredients**. The pipeline is:

### 6.1 Step 1 — Extract ingredients (subagent + INCI Decoder)
- The **ingredient subagent** (`_build_ingredient_agent`) is called from `extract_product_ingredients(...)`.
- It uses the `inci_decoder_search` tool, which queries Tavily restricted to `incidecoder.com`.
- The result is a list of **INCI-style ingredient names** (no percentages, no marketing prose), normalized by `_normalize_inci_name(...)`.

### 6.2 Step 2 — Per-ingredient safety lookup (Rules Engine + PostgreSQL)
- The flow builds a `RulesEngineRequest` with `review_type="product"` and the cleaned ingredient list.
- `RulesEngine.process(request)` (in `dermascan/rules_engine/`) **queries PostgreSQL for each ingredient** (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `POSTGRESS_PASSWORD`, `POSTGRES_TABLE`), indexing them and gathering signals such as:
  - contraindications
  - pregnancy / lactation safety
  - skin-type sensitivity
  - interaction notes
- The output `result.report` is structured, the `result.indexed_ingredients` lets us trace **which ingredient contributed what evidence**.

### 6.3 Step 3 — User-friendly summary (report subagent)
- `build_user_friendly_report(...)` calls the **report subagent** with:
  - product name, description
  - ingredient list
  - rules-engine report
  - sources
- The subagent produces a short, plain-language safety report. No bold, no italics. Practical advice, no overclaiming.

### 6.4 Step 4 — Delivery via signal flow
- The orchestrator’s signal loop receives the enriched payload.
- It composes a proactive message in the orchestrator’s voice (`soul.md`) and enqueues it on `outbound:messages`.
- Outbound sender deduplicates and delivers in correct order.

### 6.5 Why this design is the safety acknowledgement

- **Per-ingredient queries** mean we never give a blanket “this product is safe / unsafe.” Each ingredient is evaluated on its own evidence.
- **Rules engine, not freeform LLM** is the source of truth; the LLM only narrates.
- **Sources are tracked**, both from INCI lookups and the rules-engine report, so the user-facing message is grounded.
- **Conservative tone** is enforced by `soul.md` (no definitive medical claims) and by the report subagent’s system prompt.
- **Skin-review subagent** mirrors this approach for diagnoses: it researches reputable sources, refuses to prescribe, and always carries the educational disclaimer.

This is how the system meets its clinical responsibility: **safety is not a vibe — it is a per-ingredient, sourced, deterministic verdict, narrated humanely.**

---

## 7. PostgreSQL Dependency (Rules Engine)

The rules engine is **not optional** for product-safety signal flows. `RulesEngine` (`dermascan/rules_engine/engine/rules_engine.py`) opens a `psycopg2` connection at query time and reads CIR ingredient safety rows from the configured table.

**Required environment variables**

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | Database host |
| `POSTGRES_PORT` | `5432` | Port |
| `POSTGRES_DATABASE` | `postgres` | Database name |
| `POSTGRES_USER` | `postgres` | Username |
| `POSTGRESS_PASSWORD` | *(required)* | Password — note double-S spelling in code |
| `POSTGRES_TABLE` | `cir_pdfs` | Ingredient safety table |
| `REDIS_URL` | *(required)* | Ingredient index cache |

If `POSTGRESS_PASSWORD` is empty, `_connect_postgres()` raises immediately. Product and skin signal enrichment in `product_rules_signal_helper.py` will fail without a live Postgres instance populated with CIR data.

See `.env_example` at the repository root for the full variable list.

---

## 8. Cross-References

- Orchestrator stream wiring and capacity model: `claude.md` §2.
- Reliability gaps and recommended fixes (auto-reclaim, idempotency, shutdown): `claude.md` §3–§5.
- Production readiness checklist: `review.md`.
- Confidence score (informs awareness escalation): `dermascan/ai_service/modal/medgemma_service/confidence_score_algorithm.md`.
- Inference pipeline overview: `dermascan/ai_service/docs/inference.md`.

*End of `Agents.md`.*

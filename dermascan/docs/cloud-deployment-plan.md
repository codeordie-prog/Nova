# Cloud deployment plan (GCP Cloud Run)

Step-by-step plan to deploy DermaScan to GCP using **Cloud Build + Docker + Cloud Run**, while **keeping local development unchanged** (Poetry, `.env`, `do_work.py`, local MedGemma stack).

---

## Goals

| Goal | Approach |
|------|----------|
| Deploy API and workers to Cloud Run | Separate services, gcloud builds images |
| Keep local CI workflow | `.env` + same scripts; cloud uses Secret Manager |
| MedGemma GPU stays on Modal | `worker.py` + `modal_app.run()` on Cloud Run (no pre-deploy required initially) |
| Redis Cloud + external Postgres | Same URLs as today via secrets; no forced migration to Memorystore/Cloud SQL |
| Tesseract for PDF ingestion only | Separate indexer image/job — not in API/worker images |
| Minimally invasive | Add `deploy/` artifacts only; no business-logic forks for prod |

---

## Target architecture

```
Telegram / WhatsApp
        │
        ▼
┌───────────────────┐
│ Cloud Run: API    │  uvicorn dermascan.backend.app.main:app
└─────────┬─────────┘
          │ orchestrator:jobs, webhooks
          ▼
┌───────────────────┐
│ Redis Cloud       │  REDIS_URL (same as local dev)
└─────────┬─────────┘
          │
    ┌─────┴─────┬─────────────────┐
    ▼           ▼                 ▼
┌─────────┐ ┌─────────────┐ ┌─────────────────────┐
│ Cloud   │ │ Cloud Run:  │ │ Cloud Run:          │
│ Run:    │ │ medgemma-   │ │ medgemma-worker     │
│ workers │ │ gateway     │ │ worker.py + Modal   │
│do_work  │ └──────┬──────┘ └──────────┬──────────┘
└────┬────┘        │ medgemma:jobs       │ Modal RPC
     │             └────────────────────►│ GPU (Modal)
     ▼
┌─────────────┐     Optional later:
│ Postgres    │     Cloud Run Job: indexer + Tesseract
│ (external)  │     → Pinecone / Postgres CIR chunks
└─────────────┘
```

### Cloud Run services (phase 1–3)

| Service | Entrypoint | Min instances | Notes |
|---------|------------|---------------|-------|
| `dermascan-api` | `uvicorn …backend.app.main:app` | 0–1 | HTTP; set `--min-instances=1` if webhook cold starts hurt |
| `dermascan-workers` | `python dermascan/scripts/do_work.py` | **1** | Orchestrator + escalation + outbound |
| `dermascan-medgemma-gateway` | `uvicorn …gateways.endpoints:web_app` | 0–1 | Phase 3 |
| `dermascan-medgemma-worker` | `python …medgemma_service/worker.py` | **1** | Holds `modal_app.run()` session |
| `dermascan-indexer` (optional) | batch script | Job only | Tesseract + PDF chunking |

---

## Phase 0 — Prerequisites and inventory

**Duration:** 1–2 days  
**Code changes:** none

### Step 0.1 — GCP project setup

- [ ] Choose GCP project (e.g. `qutisai`) and region (e.g. `europe-west1` or `us-central1`).
- [ ] Enable APIs:
  - Cloud Run API
  - Cloud Build API
  - Artifact Registry API
  - Secret Manager API
  - (Optional) VPC Access, Cloud NAT — if Redis Cloud requires fixed egress IPs

### Step 0.2 — Artifact Registry

- [ ] Create repository: `dermascan` (format: Docker).
- [ ] Note image prefix: `{REGION}-docker.pkg.dev/{PROJECT}/dermascan/`

### Step 0.3 — Service account for Cloud Run

- [ ] Create SA: `dermascan-cloud-run@…`
- [ ] Grant minimum roles:
  - Secret Manager Secret Accessor
  - (If using Firebase/GCS from workers) Storage Object Viewer
  - (If using Vertex Gemini) Vertex AI User on project
- [ ] Cloud Build SA needs: Cloud Run Admin, SA User, Artifact Registry Writer

### Step 0.4 — Environment inventory

Map every key from `.env_example` to a **service** and **secret name**:

| Category | Examples | Used by |
|----------|----------|---------|
| Redis | `REDIS_URL` | API, all workers, MedGemma gateway/worker |
| LLM | `OPENAI_API_KEY_QUTIS`, Gemini/Vertex | Workers |
| Postgres | `POSTGRES_*` | Workers (rules engine) |
| MedGemma | `MEDGEMMA_GATEWAY_URL` | Workers (orchestrator tools) |
| Modal | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | MedGemma worker only |
| Firebase | service account JSON or ADC | API, workers, MedGemma worker |
| Webhooks | `TELEGRAM_BOT_TOKEN`, WhatsApp sidecar URL | API / ingress |
| Tesseract | `TESSERACT_PATH` | Indexer job only |

- [ ] Document staging vs production secret values (separate Secret Manager secrets or prefixes).

### Step 0.5 — Redis Cloud networking

- [ ] Confirm Redis Cloud plan supports your Cloud Run region egress.
- [ ] Enable TLS if required → use `rediss://` in `REDIS_URL`.
- [ ] **IP allowlist:** Cloud Run default egress IPs change unless you use **VPC connector + Cloud NAT** with static IP — add that IP to Redis Cloud ACL.
- [ ] Test from a one-off Cloud Run “hello” container before full deploy.

### Step 0.6 — Postgres connectivity

- [ ] Postgres is **separate from Redis** (`POSTGRES_*` in `.env_example`).
- [ ] Allow Cloud Run egress IP (or use Cloud SQL Auth Proxy / private IP if you migrate later).
- [ ] Keep `POSTGRES_SSLMODE=require` for external hosts.

### Step 0.7 — Modal credentials for cloud worker

- [ ] Create Modal token for CI/cloud: `modal token new` → store `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` in Secret Manager.
- [ ] Ensure Modal secrets exist: `huggingface`, `redis` (Modal-side, for GPU containers).
- [ ] **Current model:** no `modal deploy`; `worker.py` runs `with modal_app.run():` — document that cloud worker must stay **min instances = 1** so Modal session is stable.

---

## Phase 1 — Deploy plumbing (no production traffic)

**Duration:** 2–3 days  
**Code changes:** add `deploy/` only (Dockerfiles, cloudbuild, docs)

### Step 1.1 — Repository layout

Create (when implementing):

```
deploy/
  docker/
    Dockerfile.api
    Dockerfile.workers
    Dockerfile.medgemma-gateway   # phase 3
    Dockerfile.medgemma-worker    # phase 3
    Dockerfile.indexer            # phase 5, optional
  cloudbuild/
    cloudbuild.staging.yaml
    cloudbuild.prod.yaml
  scripts/
    promote.sh                    # optional
```

### Step 1.2 — Base Docker image strategy

- [ ] Use Python 3.12 slim base.
- [ ] Install Poetry or `pip install` from `poetry export` — match local lockfile.
- [ ] Copy `dermascan/` + `pyproject.toml`; set `WORKDIR` to repo root.
- [ ] **Do not** bake `.env` into images.
- [ ] Read Cloud Run `PORT` env with fallback `8088` (small app change when implementing).

### Step 1.3 — `Dockerfile.api`

- [ ] CMD: `uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port $PORT`
- [ ] Health check path: `GET /health`
- [ ] **Exclude Tesseract** from this image.

### Step 1.4 — `Dockerfile.workers`

- [ ] Same base image as API (shared layer cache in Cloud Build).
- [ ] CMD: `python dermascan/scripts/do_work.py`
- [ ] **Exclude Tesseract** unless you run ingestion in-process (not recommended).

### Step 1.5 — Cloud Build pipeline (staging)

- [ ] `cloudbuild.staging.yaml`:
  1. Build `dermascan-api` and `dermascan-workers` images
  2. Push to Artifact Registry
  3. Deploy to Cloud Run (staging service names, e.g. `dermascan-api-staging`)
- [ ] Trigger: manual `gcloud builds submit` first; GitHub trigger later.

Example (when implementing):

```bash
gcloud builds submit --config deploy/cloudbuild/cloudbuild.staging.yaml
```

### Step 1.6 — Secret Manager bootstrap

- [ ] Create secrets for staging (mirror `.env` keys, no commits).
- [ ] Wire Cloud Run: `--set-secrets=REDIS_URL=redis-url-staging:latest,...`

### Step 1.7 — Local dev unchanged

- [ ] Confirm `.env` still used locally; `.env` stays gitignored.
- [ ] README note: “Cloud deploy does not replace local Poetry workflow.”

---

## Phase 2 — Staging: API + backend workers

**Duration:** 2–4 days  
**Traffic:** internal / test webhooks only

### Step 2.1 — Deploy API to staging

- [ ] Deploy `dermascan-api-staging`.
- [ ] Set env: `DERMASCAN_ROOT=/app` (or equivalent in container).
- [ ] Verify: `curl https://…/health` → 200.

### Step 2.2 — Deploy workers to staging

- [ ] Deploy `dermascan-workers-staging` with **min-instances=1**, **max-instances=1**.
- [ ] Same secrets as API (Redis, LLM keys, Postgres, Firebase).
- [ ] Set `MEDGEMMA_GATEWAY_URL` to staging gateway URL (phase 3) or temporary local tunnel for smoke test.

### Step 2.3 — Staging smoke tests

- [ ] POST `/v1/orchestrator/requests` (test payload) → job lands in Redis Cloud.
- [ ] Worker logs show orchestrator consumption.
- [ ] Run existing unit tests in CI before each build (unchanged).
- [ ] Optional: run `dermascan/tests/e2e` against staging URL with `DERMASCAN_API_URL`.

### Step 2.4 — Webhook routing (staging)

- [ ] Point Telegram/WhatsApp **test bot** webhook to staging API URL (not prod).
- [ ] Or use `local_ingress_gateway.py` pattern documented for ngrok locally; staging uses public Cloud Run URL.

### Step 2.5 — Observability

- [ ] Cloud Logging: structured logs from workers already use standard logging.
- [ ] Set up log-based alerts: worker crash, 5xx on API, Redis connection errors.
- [ ] (Optional) LangSmith project per environment.

---

## Phase 3 — Staging: MedGemma gateway + worker (Modal)

**Duration:** 2–3 days

### Step 3.1 — Deploy MedGemma gateway

- [ ] `Dockerfile.medgemma-gateway` → Cloud Run service `dermascan-medgemma-gateway-staging`.
- [ ] Secrets: Redis, auth headers (`MEDGEMMA_AUTH_*` if used).
- [ ] Verify: POST to gateway enqueue endpoint → job in `medgemma:jobs` stream.

### Step 3.2 — Deploy MedGemma worker

- [ ] `Dockerfile.medgemma-worker` → Cloud Run `dermascan-medgemma-worker-staging`.
- [ ] **min-instances=1** (Modal session tied to process).
- [ ] Secrets: `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, Redis, Firebase, HF via Modal secrets.
- [ ] Entrypoint: `python dermascan/ai_service/modal/medgemma_service/worker.py`
- [ ] Logs should show: `redis ping ok` → `starting modal app context` → `worker started`.

### Step 3.3 — Wire orchestrator to staging gateway

- [ ] Set on **workers** service: `MEDGEMMA_GATEWAY_URL=https://dermascan-medgemma-gateway-staging-….run.app`
- [ ] End-to-end: skin image upload → orchestrator enqueue → gateway → worker → Modal GPU → signal back.

### Step 3.4 — Modal ops runbook

| Task | Action |
|------|--------|
| Change GPU type | Edit `app.py` `gpu=` → restart cloud medgemma-worker (or later: `modal deploy`) |
| Change `max_model_len` | Edit `config.json` → rebuild/restart worker (rebuilds Modal image on next RPC) |
| Modal auth failure | Rotate `MODAL_TOKEN_*` in Secret Manager, redeploy worker |
| Scale GPU concurrency | Tune `MEDGEMMA_MAX_INFLIGHT`, Modal `@modal.concurrent` |

### Step 3.5 — Optional: `modal deploy` hardening (later)

- [ ] One-time `modal deploy dermascan/ai_service/modal/medgemma_service/app.py`
- [ ] Worker invokes deployed app instead of relying solely on `app.run()` lifecycle
- [ ] Decouples GPU app restarts from worker pod restarts

---

## Phase 4 — Production cutover

**Duration:** 1–2 days  
**Prerequisite:** staging stable ≥ 1 week (recommended)

### Step 4.1 — Production secrets and services

- [ ] Duplicate Secret Manager secrets with `-prod` suffix.
- [ ] Deploy `dermascan-api`, `dermascan-workers`, `dermascan-medgemma-gateway`, `dermascan-medgemma-worker` (prod names, no `-staging` suffix).
- [ ] **min-instances=1** for all worker-type services.

### Step 4.2 — Production Cloud Build

- [ ] `cloudbuild.prod.yaml` — same as staging, different project/region/service names/secrets.
- [ ] Require manual approval or tag `v*` trigger for prod deploy.

### Step 4.3 — DNS / webhooks

- [ ] Point production Telegram/WhatsApp webhooks to prod API URL.
- [ ] Update any Firebase CORS / allowed origins if applicable.

### Step 4.4 — Rollback plan

- [ ] Cloud Run: revert to previous revision (`gcloud run services update-traffic … --to-revisions=PREVIOUS=100`).
- [ ] Keep previous Artifact Registry image tags for 30 days.

---

## Phase 5 — Indexer job (Tesseract / PDF chunks)

**Duration:** 1–2 days  
**When:** You need to run CIR PDF ingestion in cloud (not on every request)

### Step 5.1 — Scope

Tesseract is used in:

- `dermascan/rules_engine/vector_store/build_vectorstore.py`
- `dermascan/rules_engine/build/add_documents.py`

Runtime **product rules** queries Postgres/Pinecone — **no Tesseract at request time**.

### Step 5.2 — Indexer image

- [ ] `Dockerfile.indexer`: install `tesseract-ocr` + `eng` language pack.
- [ ] Set `TESSERACT_PATH=/usr/bin/tesseract` (Linux).
- [ ] Include `pymupdf`, `pytesseract`, `pillow` from Poetry deps.

### Step 5.3 — Cloud Run Job

- [ ] Run as **Cloud Run Job** (batch), not a always-on service.
- [ ] Trigger: manual, Cloud Scheduler, or post-deploy hook when CIR PDFs change.
- [ ] Secrets: Postgres, Pinecone, OpenAI embeddings, Firebase.

### Step 5.4 — Keep API/worker images slim

- [ ] Do **not** add Tesseract to `Dockerfile.api` or `Dockerfile.workers`.

---

## Phase 6 — CI/CD (continuous integration without disrupting local dev)

### Step 6.1 — PR pipeline (GitHub Actions or Cloud Build)

On every PR / push to main:

1. [ ] `poetry install` + `pytest dermascan/tests/unit`
2. [ ] (Optional) integration tests with fakeredis
3. [ ] **No deploy** on PR unless label `deploy-staging`

### Step 6.2 — Staging deploy

On merge to `main`:

1. [ ] Unit tests pass
2. [ ] `gcloud builds submit --config deploy/cloudbuild/cloudbuild.staging.yaml`
3. [ ] Smoke `GET /health` on staging

### Step 6.3 — Production deploy

On release tag or manual workflow:

1. [ ] Staging smoke passed
2. [ ] `gcloud builds submit --config deploy/cloudbuild/cloudbuild.prod.yaml`

### Step 6.4 — Local workflow (unchanged)

```bash
# Daily development — same as today
poetry shell
poetry run uvicorn dermascan.backend.app.main:app --port 8088 --reload
poetry run python dermascan/scripts/do_work.py
# MedGemma (when needed):
poetry run uvicorn dermascan.ai_service.modal.medgemma_service.gateways.endpoints:web_app --port 8080
poetry run python dermascan/ai_service/modal/medgemma_service/worker.py
```

Cloud deploy is **orthogonal** — never required for local feature work.

---

## Environment matrix

| Variable | Local `.env` | Cloud Run staging | Cloud Run prod |
|----------|--------------|-------------------|----------------|
| `REDIS_URL` | Redis Cloud dev DB | Redis Cloud staging DB or key prefix | Redis Cloud prod |
| `POSTGRES_*` | External IP | Same or staging DB | Prod DB |
| `MEDGEMMA_GATEWAY_URL` | `http://127.0.0.1:8080` | Staging gateway URL | Prod gateway URL |
| `MODAL_TOKEN_*` | User laptop token | Cloud SA token | Cloud SA token |
| `TESSERACT_PATH` | Windows path | `/usr/bin/tesseract` (indexer only) | Same |

Use **different Redis logical databases** or **key prefixes** for staging vs prod if sharing one Redis Cloud subscription.

---

## Checklist summary (ordered)

| # | Step | Blocker for next |
|---|------|------------------|
| 1 | GCP APIs + Artifact Registry + SA | Everything |
| 2 | Secret Manager + Redis Cloud ACL / NAT | Cloud Run deploy |
| 3 | Dockerfiles (api, workers) + cloudbuild staging | First deploy |
| 4 | Deploy API + workers staging | E2E without MedGemma |
| 5 | Deploy MedGemma gateway + worker staging | Skin review E2E |
| 6 | Staging webhook + smoke tests | Prod |
| 7 | Prod deploy + webhook cutover | Live |
| 8 | Indexer job (optional) | PDF ingestion in cloud |
| 9 | CI triggers + rollback runbook | Ongoing ops |

---

## What we explicitly defer

- Migrating Redis Cloud → Memorystore (only if ACL/cost pushes you)
- Migrating Postgres → Cloud SQL (optional later)
- Splitting `do_work.py` into three Cloud Run services (scale when needed)
- `modal deploy` separation (optional hardening after staging works)
- WhatsApp sidecar on Cloud Run (deploy separately if not already containerized)

---

## Related docs

- Local run order: repository root `README.md`
- MedGemma stack: `dermascan/ai_service/modal/medgemma_service/README.md`
- Environment keys: `.env_example`
- Cosmetic feature flags: `dermascan/docs/cosmetic-pivot.md`

---

## Next implementation task

When ready to code **Phase 1 only**:

1. Add `deploy/docker/Dockerfile.api` and `Dockerfile.workers`
2. Add `deploy/cloudbuild/cloudbuild.staging.yaml`
3. Small change: read `PORT` in uvicorn entrypoint
4. Add `docs/deploy-operations.md` with gcloud commands for your project/region

Do **not** change orchestrator logic, Modal app, or local scripts in Phase 1.

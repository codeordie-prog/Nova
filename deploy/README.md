# DermaScan — GCP deploy guide (backend only)

Step-by-step guide for **Cloud Build + Artifact Registry + Cloud Run**. Frontend is out of scope.

## What gets deployed

| Cloud Run service | Image target | Role |
|-------------------|--------------|------|
| `dermascan-api-staging` | `api` | FastAPI ingress, webhooks, `/health` |
| `dermascan-workers-staging` | `workers` | `do_work.py` (orchestrator, escalation, outbound) |
| `dermascan-telegram-bridge-staging` | `telegram-bridge` | Polls Telegram → forwards to API webhook |
| `dermascan-medgemma-gateway-staging` | `medgemma-gateway` | Enqueue skin diagnosis jobs |
| `dermascan-medgemma-worker-staging` | `medgemma-worker` | Redis consumer → Modal GPU |

**Telegram in cloud:** this setup uses the **polling bridge** for MVP (same as local dev). Later you can switch to Telegram `setWebhook` pointing at `https://<api-url>/v1/webhooks/telegram` and stop the bridge service.

**MedGemma cost:** Modal app uses `min_containers=0` — GPU scales down when idle. Cloud Run workers can use `min-instances=0` between demo sessions.

---

## Phase 0 — One-time GCP setup

Replace placeholders:

- `PROJECT_ID` → e.g. `qutisai`
- `REGION` → e.g. `europe-west1` (match Postgres/Redis latency)

### 0.1 Select project and enable APIs

```bash
gcloud config set project PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com
```

### 0.2 Artifact Registry (Docker images)

```bash
gcloud artifacts repositories create dermascan \
  --repository-format=docker \
  --location=REGION \
  --description="DermaScan backend images"

gcloud auth configure-docker REGION-docker.pkg.dev
```

Image prefix: `REGION-docker.pkg.dev/PROJECT_ID/dermascan/`

### 0.3 Cloud Run service account

```bash
gcloud iam service-accounts create dermascan-cloud-run \
  --display-name="DermaScan Cloud Run"

export RUN_SA="dermascan-cloud-run@PROJECT_ID.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/aiplatform.user"
```

Add Storage roles if workers read Firebase/GCS buckets.

### 0.4 Cloud Build permissions

Cloud Build uses the **Compute Engine default service account** or a custom builder SA. Grant it permission to push images and deploy Cloud Run:

```bash
export PROJECT_NUMBER="$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')"
export CLOUD_BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:${CLOUD_BUILD_SA}" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:${CLOUD_BUILD_SA}" \
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:${CLOUD_BUILD_SA}" \
  --role="roles/artifactregistry.writer"
```

Without these, builds succeed but deploy steps fail with permission errors.

### 0.5 Secret Manager — sync full `.env` (required before staging deploy)

Cloud Run does **not** use your local `.env` file. Upload it once with:

```bash
cd C:\Users\LENOVO\Documents\dermascanAgents
python deploy/scripts/sync_staging_secrets.py --project qutisai
```

This creates/updates:

| Secret | Contents |
|--------|----------|
| `dermascan-dotenv-staging` | **Entire `.env`** with cloud path overrides |
| `dermascan-google-credentials-staging` | Google service account JSON |
| `dermascan-firebase-config-staging` | Firebase config JSON |

At runtime each service mounts `/secrets/env/.env` and loads it via `DERMASCAN_ENV_FILE`
(one directory per secret — Cloud Run allows only one secret per mount directory).
Deploy also sets live URLs: `MEDGEMMA_GATEWAY_URL`, `TELEGRAM_LOCAL_WEBHOOK_URL`.

Re-run the sync script whenever you change `.env`.

### 0.6 Redis Cloud + Postgres network

Cloud Run egress IPs are **not fixed** unless you add VPC connector + Cloud NAT.

1. **Redis Cloud** — allow Cloud Run egress IPs or enable VPC/NAT with a static IP on the allowlist.
2. **Postgres** — allow the same egress (your Cloud SQL public IP / external Postgres host).
3. Test connectivity with a one-off Cloud Run job before full deploy if unsure.

### 0.7 Modal (MedGemma worker)

On your laptop:

```bash
modal token new
```

Store `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in Secret Manager (step 0.5).

Ensure Modal secrets exist: `huggingface`, `redis` (Modal-side for GPU containers).

Optional (recommended for cost): deploy Modal app once:

```bash
modal deploy dermascan/ai_service/modal/medgemma_service/app.py
```

---

## Phase 1 — Build images only (first run)

Validates Docker builds **without** deploying services.

From repo root:

```bash
gcloud builds submit \
  --config=deploy/cloudbuild/cloudbuild.images-only.yaml \
  --substitutions=_REGION=REGION,_TAG=latest
```

Expected: ~15–30 min first build (torch in lockfile). Uses `E2_HIGHCPU_8` and 100 GB disk.

Verify images:

```bash
gcloud artifacts docker images list REGION-docker.pkg.dev/PROJECT_ID/dermascan
```

### Local Docker test (optional)

```bash
docker build --target api -f deploy/docker/Dockerfile -t dermascan-api:local .
docker run --rm -p 8080:8080 --env-file .env dermascan-api:local
curl http://127.0.0.1:8080/health
```

---

## Phase 2 — Deploy staging (Cloud Build)

After secrets exist and Phase 0 IAM is done:

```bash
gcloud builds submit \
  --config=deploy/cloudbuild/cloudbuild.staging.yaml \
  --substitutions=_REGION=REGION,_TAG=latest,_POSTGRES_HOST=YOUR_POSTGRES_IP
```

This will:

1. Build all five images
2. Push to Artifact Registry
3. Run `deploy/scripts/deploy-staging-services.sh`:
   - Deploy API → read URL
   - Deploy MedGemma gateway → read URL → inject into workers
   - Deploy workers + medgemma-worker
   - Deploy telegram bridge with `TELEGRAM_LOCAL_WEBHOOK_URL=<api>/v1/webhooks/telegram`

### Smoke test

```bash
API_URL="$(gcloud run services describe dermascan-api-staging \
  --region=REGION --format='value(status.url)')"
curl "${API_URL}/health"
```

Scale up for a demo session:

```bash
for svc in dermascan-api-staging dermascan-workers-staging \
  dermascan-medgemma-gateway-staging dermascan-medgemma-worker-staging \
  dermascan-telegram-bridge-staging; do
  gcloud run services update "$svc" --region=REGION --min-instances=1
done
```

Scale down when idle (save cost):

```bash
for svc in dermascan-api-staging dermascan-workers-staging \
  dermascan-medgemma-gateway-staging dermascan-medgemma-worker-staging \
  dermascan-telegram-bridge-staging; do
  gcloud run services update "$svc" --region=REGION --min-instances=0
done
```

---

## How Cloud Build is structured

```
deploy/cloudbuild/
  cloudbuild.images-only.yaml   # build + push only
  cloudbuild.staging.yaml       # build + push + deploy staging
```

### Substitutions (override on CLI)

| Variable | Default | Purpose |
|----------|---------|---------|
| `_REGION` | `europe-west1` | Artifact Registry + Cloud Run region |
| `_REPO` | `dermascan` | Artifact Registry repo name |
| `_TAG` | `latest` | Image tag |
| `_RUN_SA` | `dermascan-cloud-run@PROJECT_ID.iam.gserviceaccount.com` | Cloud Run runtime SA |
| `_POSTGRES_HOST` | (your IP) | External Postgres host for workers |
| `_SECRETS_SPEC` | (empty) | Override full `--set-secrets` list |

Example with commit SHA tag:

```bash
gcloud builds submit \
  --config=deploy/cloudbuild/cloudbuild.staging.yaml \
  --substitutions=_REGION=europe-west1,_TAG=$(git rev-parse --short HEAD),_POSTGRES_HOST=34.79.111.107
```

### GitHub trigger (later)

In GCP Console → Cloud Build → Triggers:

- **Event:** Push to `main`
- **Config:** `deploy/cloudbuild/cloudbuild.staging.yaml`
- **Substitutions:** set `_REGION`, `_POSTGRES_HOST`

Use `cloudbuild.images-only.yaml` on PRs if you only want build validation.

---

## Cloud Run service notes

| Service | Public? | min-instances (MVP) |
|---------|---------|---------------------|
| API | Yes (`--allow-unauthenticated`) | 0 |
| MedGemma gateway | Yes | 0 |
| Workers | No | 0 (scale up for demos) |
| MedGemma worker | No | 0 |
| Telegram bridge | No | 0 |

Workers use `deploy/scripts/run_with_health.py` to expose `GET /health` on `$PORT` (Cloud Run requirement).

Workers image includes **Tesseract** (`TESSERACT_PATH=/usr/bin/tesseract`) for lazy PDF indexing.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Build OOM / disk full | Increase `diskSizeGb` in cloudbuild yaml (already 100) |
| Cloud Run deploy permission denied | Step 0.4 Cloud Build IAM |
| Redis connection refused | Redis Cloud IP allowlist / VPC |
| Google auth failed | Mount `dermascan-google-credentials-staging` at `/secrets/google/credentials.json` |
| OpenAI 429 on every turn | Update `ORCHESTRATOR_MODEL_CHAIN` in secrets/env — Gemini should be first |
| MedGemma cold start slow | Expected with `min_containers=0`; first image ~2–5 min |
| Telegram duplicate messages | Don't run polling bridge **and** webhook simultaneously |

---

## Local dev unchanged

Cloud deploy is additive. Local workflow stays:

```bash
poetry run uvicorn dermascan.backend.app.main:app --port 8088 --reload
poetry run python dermascan/scripts/do_work.py
```

See also: [cloud-deployment-plan.md](../dermascan/docs/cloud-deployment-plan.md)

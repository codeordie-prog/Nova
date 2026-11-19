#!/usr/bin/env bash
# Deploy DermaScan staging Cloud Run services after images are pushed.
#
# Prerequisites:
#   python deploy/scripts/sync_staging_secrets.py --project "$PROJECT_ID"
set -euo pipefail

REGION="${REGION:?REGION is required}"
PROJECT_ID="${PROJECT_ID:?PROJECT_ID is required}"
REPO="${REPO:-dermascan}"
TAG="${TAG:-latest}"
RUN_SA="${RUN_SA:-dermascan-cloud-run@${PROJECT_ID}.iam.gserviceaccount.com}"
# Postgres (three modes):
# 1) Cross-project / in-project Cloud SQL socket — set CLOUDSQL_CONNECTION (e.g. qutisai:europe-west1:qutis).
#    RUN_SA needs roles/cloudsql.client on the project that owns the instance.
# 2) External TCP — set POSTGRES_HOST (public IP); instance must allow Cloud Run egress (authorized networks / NAT).
# 3) Default socket in deploy PROJECT_ID — ${PROJECT_ID}:${REGION}:qutis
CLOUDSQL_CONNECTION="${CLOUDSQL_CONNECTION:-}"
cloudsql_flags=()
if [[ -n "${CLOUDSQL_CONNECTION}" ]]; then
  cloudsql_flags=( --add-cloudsql-instances="${CLOUDSQL_CONNECTION}" )
  POSTGRES_CLOUD_ENV="POSTGRES_HOST=/cloudsql/${CLOUDSQL_CONNECTION},POSTGRES_SSLMODE=disable"
  echo "Postgres: Cloud SQL socket ${CLOUDSQL_CONNECTION}"
elif [[ -n "${POSTGRES_HOST:-}" ]]; then
  POSTGRES_CLOUD_ENV="POSTGRES_HOST=${POSTGRES_HOST},POSTGRES_SSLMODE=${POSTGRES_SSLMODE:-require}"
  echo "Postgres: TCP ${POSTGRES_HOST} (no Cloud SQL socket mount)"
else
  CLOUDSQL_CONNECTION="${PROJECT_ID}:${REGION}:qutis"
  cloudsql_flags=( --add-cloudsql-instances="${CLOUDSQL_CONNECTION}" )
  POSTGRES_CLOUD_ENV="POSTGRES_HOST=/cloudsql/${CLOUDSQL_CONNECTION},POSTGRES_SSLMODE=disable"
  echo "Postgres: Cloud SQL socket ${CLOUDSQL_CONNECTION} (default)"
fi

IMAGE_PREFIX="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
DOTENV_SECRET="dermascan-dotenv-staging"

if ! gcloud secrets describe "${DOTENV_SECRET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "ERROR: Secret ${DOTENV_SECRET} not found."
  echo "Run first: python deploy/scripts/sync_staging_secrets.py --project ${PROJECT_ID}"
  exit 1
fi

# Full .env (Secret Manager file) + credential JSON mounts.
# Cloud Run allows only ONE secret per mount directory, so each gets its own dir.
SECRETS_BLOCK="/secrets/env/.env=${DOTENV_SECRET}:latest,/secrets/google/credentials.json=dermascan-google-credentials-staging:latest,/secrets/firebase/config.json=dermascan-firebase-config-staging:latest"

if [[ -n "${SECRETS_SPEC:-}" ]]; then
  SECRETS_BLOCK="${SECRETS_SPEC}"
fi

common_flags=(
  --region="${REGION}"
  --platform=managed
  --service-account="${RUN_SA}"
  --set-secrets="${SECRETS_BLOCK}"
)

# Process-wide billing Postgres pools need CPU between HTTP requests so TCP
# keepalives and pool checkout checks run; default Cloud Run throttling freezes
# idle instances and stale sockets show up as psycopg OperationalError.
no_cpu_throttle=( --no-cpu-throttling )

echo "Deploying dermascan-api-staging..."
gcloud run deploy dermascan-api-staging \
  --image="${IMAGE_PREFIX}/dermascan-api:${TAG}" \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=2Gi \
  --min-instances=1 \
  --max-instances=1 \
  --timeout=300 \
  "${cloudsql_flags[@]}" \
  --set-env-vars="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app,${POSTGRES_CLOUD_ENV}" \
  "${no_cpu_throttle[@]}" \
  "${common_flags[@]}"

API_URL="$(gcloud run services describe dermascan-api-staging \
  --region="${REGION}" \
  --format='value(status.url)')"
echo "API URL: ${API_URL}"

echo "Deploying dermascan-medgemma-gateway-staging..."
# min-instances=1: cold starts exceed the orchestrator health-probe timeout,
# which makes check_medgemma_health report the gateway as unavailable.
gcloud run deploy dermascan-medgemma-gateway-staging \
  --image="${IMAGE_PREFIX}/dermascan-medgemma-gateway:${TAG}" \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=2Gi \
  --min-instances=1 \
  --max-instances=1 \
  --timeout=300 \
  "${cloudsql_flags[@]}" \
  --set-env-vars="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app,${POSTGRES_CLOUD_ENV}" \
  "${no_cpu_throttle[@]}" \
  "${common_flags[@]}"

MEDGEMMA_GATEWAY_URL="$(gcloud run services describe dermascan-medgemma-gateway-staging \
  --region="${REGION}" \
  --format='value(status.url)')"
echo "MedGemma gateway URL: ${MEDGEMMA_GATEWAY_URL}"

RUNTIME_OVERRIDES="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app,MEDGEMMA_GATEWAY_URL=${MEDGEMMA_GATEWAY_URL},TELEGRAM_LOCAL_WEBHOOK_URL=${API_URL}/v1/webhooks/telegram"

# Staging cost cap: every service uses max-instances=1 (no horizontal burst).
# Background consumers (Redis streams / Telegram polling) get no HTTP traffic,
# so min-instances=1 is required or Cloud Run never runs them.
worker_flags=(
  --no-allow-unauthenticated
  --port=8080
  --cpu=2
  --memory=4Gi
  --min-instances=1
  --max-instances=1
  --timeout=3600
  "${no_cpu_throttle[@]}"
)

echo "Deploying dermascan-workers-staging..."
gcloud run deploy dermascan-workers-staging \
  --image="${IMAGE_PREFIX}/dermascan-workers:${TAG}" \
  "${cloudsql_flags[@]}" \
  --set-env-vars="${RUNTIME_OVERRIDES},${POSTGRES_CLOUD_ENV}" \
  "${worker_flags[@]}" \
  "${common_flags[@]}"

echo "Deploying dermascan-medgemma-worker-staging..."
# MedGemma worker is a Redis consumer; it does not need the Cloud SQL socket mount.
# Socket mount + min-instances=1 has caused startup probe timeouts on some revisions
# while traffic was pinned to an older revision. Postgres for billing comes from the
# mounted staging .env (TCP or socket vars as synced by sync_staging_secrets.py).
gcloud run deploy dermascan-medgemma-worker-staging \
  --image="${IMAGE_PREFIX}/dermascan-medgemma-worker:${TAG}" \
  --clear-cloudsql-instances \
  --set-env-vars="${RUNTIME_OVERRIDES}" \
  "${worker_flags[@]}" \
  "${common_flags[@]}"

echo "Deploying dermascan-telegram-bridge-staging..."
gcloud run deploy dermascan-telegram-bridge-staging \
  --image="${IMAGE_PREFIX}/dermascan-telegram-bridge:${TAG}" \
  --set-env-vars="${RUNTIME_OVERRIDES}" \
  --no-allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=512Mi \
  --min-instances=1 \
  --max-instances=1 \
  --timeout=3600 \
  "${no_cpu_throttle[@]}" \
  "${common_flags[@]}"

echo "Staging deploy complete."
echo "  API:              ${API_URL}"
echo "  MedGemma gateway: ${MEDGEMMA_GATEWAY_URL}"
echo "  Health:           curl ${API_URL}/health"

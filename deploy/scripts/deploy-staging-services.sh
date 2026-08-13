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
# Cloud SQL over unix socket (no public-IP allowlisting; RUN_SA needs roles/cloudsql.client).
CLOUDSQL_CONNECTION="${CLOUDSQL_CONNECTION:-${PROJECT_ID}:${REGION}:qutis}"

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

echo "Deploying dermascan-api-staging..."
gcloud run deploy dermascan-api-staging \
  --image="${IMAGE_PREFIX}/dermascan-api:${TAG}" \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=2Gi \
  --min-instances=0 \
  --max-instances=3 \
  --timeout=300 \
  --set-env-vars="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app" \
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
  --max-instances=2 \
  --timeout=300 \
  --set-env-vars="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app" \
  "${common_flags[@]}"

MEDGEMMA_GATEWAY_URL="$(gcloud run services describe dermascan-medgemma-gateway-staging \
  --region="${REGION}" \
  --format='value(status.url)')"
echo "MedGemma gateway URL: ${MEDGEMMA_GATEWAY_URL}"

RUNTIME_OVERRIDES="DERMASCAN_ENV_FILE=/secrets/env/.env,DERMASCAN_ROOT=/app,MEDGEMMA_GATEWAY_URL=${MEDGEMMA_GATEWAY_URL},TELEGRAM_LOCAL_WEBHOOK_URL=${API_URL}/v1/webhooks/telegram"

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
  --no-cpu-throttling
)

echo "Deploying dermascan-workers-staging..."
gcloud run deploy dermascan-workers-staging \
  --image="${IMAGE_PREFIX}/dermascan-workers:${TAG}" \
  --add-cloudsql-instances="${CLOUDSQL_CONNECTION}" \
  --set-env-vars="${RUNTIME_OVERRIDES},POSTGRES_HOST=/cloudsql/${CLOUDSQL_CONNECTION},POSTGRES_SSLMODE=disable" \
  "${worker_flags[@]}" \
  "${common_flags[@]}"

echo "Deploying dermascan-medgemma-worker-staging..."
gcloud run deploy dermascan-medgemma-worker-staging \
  --image="${IMAGE_PREFIX}/dermascan-medgemma-worker:${TAG}" \
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
  --no-cpu-throttling \
  "${common_flags[@]}"

echo "Staging deploy complete."
echo "  API:              ${API_URL}"
echo "  MedGemma gateway: ${MEDGEMMA_GATEWAY_URL}"
echo "  Health:           curl ${API_URL}/health"

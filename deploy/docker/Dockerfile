# DermaScan backend images (multi-target).
# Build examples:
#   docker build --target api     -f deploy/docker/Dockerfile .
#   docker build --target workers -f deploy/docker/Dockerfile .

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.1.1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    DERMASCAN_ROOT=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock README.md ./
COPY dermascan ./dermascan
COPY deploy ./deploy
COPY turn ./turn

# MVP: full runtime dependency set from lockfile (torch/modal included).
# Split optional groups later to slim api-only images.
RUN poetry install --without dev --no-ansi --no-root \
    && poetry install --only-root --no-ansi


# ---------------------------------------------------------------------------
# API — FastAPI ingress (Telegram/WhatsApp webhooks, orchestrator enqueue)
# ---------------------------------------------------------------------------
FROM base AS api

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port ${PORT}"]


# ---------------------------------------------------------------------------
# Workers — orchestrator + escalation + outbound (do_work.py)
# ---------------------------------------------------------------------------
FROM base AS workers

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

ENV PORT=8080 \
    TESSERACT_PATH=/usr/bin/tesseract

EXPOSE 8080

CMD ["python", "deploy/scripts/run_workers_with_turn_sidecar.py"]


# ---------------------------------------------------------------------------
# Telegram polling bridge — forwards getUpdates to API webhook (MVP / staging)
# Prefer Telegram setWebhook → API URL in production when stable.
# ---------------------------------------------------------------------------
FROM base AS telegram-bridge

ENV PORT=8080

EXPOSE 8080

CMD ["python", "deploy/scripts/run_with_health.py", "python", "dermascan/backend/services/agents/telegram_polling_bridge.py"]


# ---------------------------------------------------------------------------
# MedGemma gateway — enqueue FastAPI (:8080)
# ---------------------------------------------------------------------------
FROM base AS medgemma-gateway

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn dermascan.ai_service.modal.medgemma_service.gateways.endpoints:web_app --host 0.0.0.0 --port ${PORT}"]


# ---------------------------------------------------------------------------
# MedGemma worker — Redis consumer + Modal GPU RPC
# ---------------------------------------------------------------------------
FROM base AS medgemma-worker

ENV PORT=8080

EXPOSE 8080

CMD ["python", "deploy/scripts/run_with_health.py", "python", "dermascan/ai_service/modal/medgemma_service/worker.py"]

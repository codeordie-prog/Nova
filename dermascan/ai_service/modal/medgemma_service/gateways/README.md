# MedGemma Gateway API (FastAPI)

This folder contains the **FastAPI gateway layer** that accepts requests and enqueues jobs for asynchronous processing.

It is responsible for:
- validating image uploads (size + basic format signature)
- translating validation errors into clean HTTP errors
- protecting the backend via **rate limiting** before enqueue
- enqueuing jobs to Redis Streams for worker-side processing
- exposing job status polling endpoints

## Files

- **`endpoints.py`**
  FastAPI app + enqueue/status endpoints.

- **`validation.py`**
  Image validation helpers.

- **`custom_exceptions.py`**
  Domain exceptions used by validation (mapped to HTTP in `endpoints.py`).

- **`rate_limiter.py`**
  In-memory per-process rate limiter.

- **`config.json`**
  Gateway config for request rate limiting.

## Configuration

Edit `config.json`:
- `rate_limiter.max_requests_per_minute`
- `rate_limit_window`

Notes:
- Rate limiting is **in-memory** and **per-process**. If you run multiple replicas, each replica enforces its own limits.

## Run locally (gateway)

From the repo root (where `pyproject.toml` lives):

```bash
poetry install
poetry shell
poetry run python -m uvicorn dermascan.ai_service.modal.medgemma_service.gateways.endpoints:web_app --host 0.0.0.0 --port 8080 --reload
```

This starts the FastAPI gateway locally.

## Run with worker

In a second terminal, run the worker so queued jobs are consumed and processed:

```bash
python dermascan/ai_service/modal/medgemma_service/worker.py
```

The worker dispatches inference to Modal `MedGemmaService` GPU containers.

## API endpoints

- `POST /diagnose`
  - form-data: `image` (file), `prompt` (string)

- `POST /diagnose_batch`
  - form-data: `images` (files[]), `prompts` (strings[])

- `POST /diagnose_base64`
  - json: `{ "image_base64": "...", "prompt": "..." }`

- `GET /jobs/{job_id}`
  - fetch current job status/result from Redis

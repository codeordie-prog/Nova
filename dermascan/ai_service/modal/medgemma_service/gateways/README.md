# MedGemma Gateway API (FastAPI)

This folder contains the **FastAPI gateway layer** that sits *above* the Modal RPC calls.

It is responsible for:
- validating image uploads (size + basic format signature)
- translating validation errors into clean HTTP errors
- protecting the inference backend via:
  - **rate limiting** (per-IP)
  - **concurrency limiting** (semaphore)
  - **timeouts** (fail fast when inference is slow)

## Files

- **`endpoints.py`**
  FastAPI app + endpoints. Exposed via `@modal.asgi_app()`.

- **`validation.py`**
  Image validation helpers.

- **`custom_exceptions.py`**
  Domain exceptions used by validation (mapped to HTTP in `endpoints.py`).

- **`rate_limiter.py`**
  In-memory rate limiter + concurrency semaphore + timeout wrapper.

- **`config.json`**
  Simple gateway config for rate limiting / timeouts / concurrency.

## Configuration

Edit `config.json`:
- `rate_limiter.max_requests_per_minute`
- `rate_limit_window`
- `max_concurrency`
- `timeout`

Notes:
- Rate limiting is **in-memory** and **per-process**. If you run multiple replicas, each replica enforces its own limits.

## Run locally (gateway)

From the repo root (where `pyproject.toml` lives):

```bash
poetry install
poetry shell
modal token new
modal secret create huggingface HF_TOKEN=YOUR_TOKEN

modal serve dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py
```

This starts the gateway and prints the local URL.

## Deploy / run on Modal

You can run the gateway on Modal using the same entrypoint:

```bash
modal deploy dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py
```

Modal will provide the public URL for the ASGI app.

## API endpoints

- `POST /diagnose`
  - form-data: `image` (file), `prompt` (string)

- `POST /diagnose_batch`
  - form-data: `images` (files[]), `prompts` (strings[])

- `POST /diagnose_base64`
  - json: `{ "image_base64": "...", "prompt": "..." }`

# MedGemma Service (Modal)

This package contains the **Modal-hosted MedGemma inference service** and the **FastAPI gateway** that validates inputs and forwards requests to Modal.

## What lives here

- **`app.py`**
  Modal app + `MedGemmaService` class.
  - Model is loaded once per container in `@modal.enter()`.
  - Inference methods are exposed via `@modal.method()` (`run_one`, `run_batch`).

- **`model.py`**
  Model loader (pipeline construction only).

- **`schemas.py`**
  Pydantic request/response schemas used by the gateway API.

- **`gateways/`**
  FastAPI ASGI app exposed via Modal (`modal serve ...`).
  Includes input validation + rate limiting + concurrency control + timeouts.

## Request flow (high level)

1. Client calls FastAPI gateway endpoint (`/diagnose`, `/diagnose_batch`, `/diagnose_base64`).
2. Gateway validates prompt + image payload and applies:
   - rate limiting
   - concurrency limit
   - request timeout
3. Gateway calls `MedGemmaService.run_one.remote(...)` or `run_batch.remote(...)`.
4. Modal executes inference in the GPU container and returns `(boot_id, text)`.

## How to run

See `gateways/README.md` for how to run the gateway API locally and on Modal.

# MedGemma Service (Modal)

This package contains the **Modal-hosted MedGemma inference service**, a **FastAPI enqueue API**, and a **worker** that consumes queued jobs and performs inference.

## What lives here

- **`app.py`**
  Modal app + `MedGemmaService` class.
  - Model is loaded once per container in `@modal.enter()`.
  - Inference methods are exposed via `@modal.method()` (`run_one`, `run_batch`).

- **`model.py`**
  Model loader (pipeline construction only).

- **`schemas.py`**
  Pydantic request/response schemas used by the enqueue API.

- **`worker.py`**
  Redis Stream consumer that:
  - reads queued jobs from the stream
  - dispatches inference to `MedGemmaService`
  - writes job result/status back to Redis

- **`gateways/`**
  FastAPI enqueue/status API served with `uvicorn`.
  Includes input validation and request rate limiting before enqueue.

## Request flow (high level)

1. Client calls FastAPI gateway endpoint (`/diagnose`, `/diagnose_batch`, `/diagnose_base64`).
2. Gateway validates payload and enqueues a Redis Stream job.
3. Worker consumes jobs and dispatches `MedGemmaService.run_one` on Modal GPU containers.
4. Worker updates Redis job status (`queued` -> `running` -> `done`/`failed`).
5. Client polls `GET /jobs/{job_id}` for result.

## How to run

See:
- `gateways/README.md` for running the API with `uvicorn`
- project root `README.md` for end-to-end API + worker quickstart

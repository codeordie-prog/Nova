# Dermascan Agents

AI agents network for medical diagnosis.

## MedGemma Async Quickstart

The MedGemma service now runs as a separated async system:

- **FastAPI API (`uvicorn`)**: validates requests and enqueues jobs to Redis.
- **Worker process**: consumes Redis Stream jobs and calls Modal GPU inference.
- **Modal service (`app.py`)**: hosts `MedGemmaService` GPU methods used by workers.

### Prerequisites

- Python 3.12+
- Poetry
- Modal CLI (required for worker inference calls)
- Redis instance (for queue + job status storage)

### Setup

```bash
poetry install
poetry shell

modal token new
modal secret create huggingface HF_TOKEN=YOUR_TOKEN
modal secret create redis REDIS_HOST=YOUR_HOST REDIS_PORT=YOUR_PORT REDIS_USERNAME=YOUR_USERNAME REDIS_PASSWORD=YOUR_PASSWORD REDIS_DB=YOUR_DB
```

### Run the API locally (uvicorn)

```bash
poetry run python -m uvicorn dermascan.ai_service.modal.medgemma_service.gateways.endpoints:web_app --host 0.0.0.0 --port 8080 --reload
```

### Run the worker

```bash
python dermascan/ai_service/modal/medgemma_service/worker.py
```

### Configuration

Gateway/API and worker limits are configured via env vars and gateway config:

`dermascan/ai_service/modal/medgemma_service/gateways/config.json`

See:
- `dermascan/ai_service/modal/medgemma_service/README.md`
- `dermascan/ai_service/modal/medgemma_service/gateways/README.md`

## Project Structure

- **frontend**: Web UI and client-side application.
- **backend**: API server, business logic, and persistence.
- **ai-service**: Model serving, inference pipelines, and evaluation.
- **data**: Sample data, datasets, and evaluation sets.
- **infra**: Infrastructure-as-code, deployment, and monitoring.
- **scripts**: Setup, migrations, and maintenance utilities.
- **docs**: Architecture, API docs, and research notes.
- **tests**: Integration and end-to-end tests.

See each subdirectory `README.md` for more details.

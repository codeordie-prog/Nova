# Dermascan Agents

AI agents network for medical diagnosis.

## Modal (MedGemma) Quickstart

The MedGemma inference service is implemented as a **Modal app** and exposed via a **FastAPI gateway**.

- Entry point:
  `dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py`

### Prerequisites

- Python 3.12+
- Poetry
- Modal CLI (installed by Poetry via the `modal` dependency)

### Setup

```bash
poetry install
poetry shell

modal token new
modal secret create huggingface HF_TOKEN=YOUR_TOKEN
```

### Run the gateway locally

```bash
modal serve dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py
```

### Deploy the gateway to Modal

```bash
modal deploy dermascan/ai_service/modal/medgemma_service/gateways/endpoints.py
```

### Configuration

Gateway limits (rate limiting / concurrency / timeout) are configured in:

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

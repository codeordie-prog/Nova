from fastapi import FastAPI

from dermascan.backend.api.endpoints import router as orchestrator_router


app = FastAPI(title="Qutis Backend API", version="0.1")
app.include_router(orchestrator_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

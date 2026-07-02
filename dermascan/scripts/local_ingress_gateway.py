"""
Local ingress gateway for single-tunnel ngrok dev.

Routes public traffic on one port to the WhatsApp sidecar (:8000) and Nova API (:8088):

- GET/POST /webhook*  -> qutis-whatsapp-api
- /v1/*, /health      -> Nova backend

Run:
    poetry run python dermascan/scripts/local_ingress_gateway.py

Then: ngrok http 9000
"""

from __future__ import annotations

import os
from typing import Iterable

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

load_dotenv()

WHATSAPP_SIDECAR_URL: str = os.getenv("WHATSAPP_SIDECAR_URL", "http://127.0.0.1:8000").rstrip("/")
NOVA_API_URL: str = os.getenv("NOVA_API_URL", "http://127.0.0.1:8088").rstrip("/")
INGRESS_GATEWAY_HOST: str = os.getenv("INGRESS_GATEWAY_HOST", "0.0.0.0")
INGRESS_GATEWAY_PORT: int = int(os.getenv("INGRESS_GATEWAY_PORT", "9000"))
INGRESS_GATEWAY_TIMEOUT_S: float = float(os.getenv("INGRESS_GATEWAY_TIMEOUT_S", "120"))

_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

app = FastAPI(title="Dermascan Local Ingress Gateway", version="0.1.0")


def _filtered_request_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """
    Copy inbound headers for upstream forwarding, dropping hop-by-hop fields.

    Args:
        headers: Incoming header pairs from the client request.

    Returns:
        dict[str, str]: Headers safe to forward upstream.
    """
    out: dict[str, str] = {}
    for key, value in headers:
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


async def _proxy_request(request: Request, upstream_base: str, upstream_path: str) -> Response:
    """
    Forward an HTTP request to an upstream base URL and return its response.

    Args:
        request: Incoming FastAPI request.
        upstream_base: Origin without trailing slash (e.g. http://127.0.0.1:8088).
        upstream_path: Path including leading slash on the upstream service.

    Returns:
        Response: Proxied status, headers, and body.
    """
    query = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{upstream_base}{upstream_path}{query}"
    body = await request.body()
    headers = _filtered_request_headers(request.headers.items())

    async with httpx.AsyncClient(timeout=INGRESS_GATEWAY_TIMEOUT_S) as client:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=headers,
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/")
async def gateway_index() -> dict[str, str | list[str]]:
    """
    Describe routing for local ngrok setup.

    Returns:
        dict[str, str | list[str]]: Gateway metadata and route map.
    """
    return {
        "status": "ok",
        "gateway_port": str(INGRESS_GATEWAY_PORT),
        "routes": [
            f"/webhook -> {WHATSAPP_SIDECAR_URL}/webhook  (360dialog)",
            f"/v1/*   -> {NOVA_API_URL}/v1/*  (Nova API + dashboard return webhook)",
            f"/health -> {NOVA_API_URL}/health",
        ],
        "ngrok": f"ngrok http {INGRESS_GATEWAY_PORT}",
        "whatsapp_webhook_url": "https://<ngrok-host>/webhook",
        "dashboard_nova_callback": "https://<ngrok-host>/v1/webhooks/escalation",
    }


@app.api_route("/health", methods=["GET", "HEAD"])
async def proxy_health(request: Request) -> Response:
    """Proxy Nova health check."""
    return await _proxy_request(request, NOVA_API_URL, "/health")


@app.api_route("/webhook", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/webhook/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_webhook(request: Request, path: str = "") -> Response:
    """Proxy WhatsApp sidecar webhook (360dialog ingress)."""
    upstream_path = "/webhook" if not path else f"/webhook/{path}"
    return await _proxy_request(request, WHATSAPP_SIDECAR_URL, upstream_path)


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_nova_v1(request: Request, path: str) -> Response:
    """Proxy Nova /v1 routes (WhatsApp ingress from sidecar, escalation return webhook, etc.)."""
    return await _proxy_request(request, NOVA_API_URL, f"/v1/{path}")


def main() -> None:
    """Run the ingress gateway with uvicorn."""
    uvicorn.run(
        app,
        host=INGRESS_GATEWAY_HOST,
        port=INGRESS_GATEWAY_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()

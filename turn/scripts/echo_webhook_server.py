"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Minimal Turn.io webhook echo server.

Implements exactly the contract in https://whatsapp.turn.io/docs/api/webhooks:

- Accepts POST webhook events (any path: ``/`` and ``/webhook``).
- Logs the ``X-Turn-Hook-Subscription`` and ``X-Turn-Hook-Signature`` headers.
- Optionally verifies the HMAC-SHA256 signature (``TURN_WEBHOOK_SECRET``).
- On an inbound contact message (``whatsapp`` subscription, ``messages[]`` with
  a ``from`` field), replies with a simple text via ``POST /v1/messages``.

Run (ngrok already targets port 8088):

    poetry run uvicorn turn.scripts.echo_webhook_server:app --host 0.0.0.0 --port 8088

No Nova, Redis, or workers involved.
"""


from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger: logging.Logger = logging.getLogger("turn-echo")

TURN_API_TOKEN: str = os.getenv("TURN_API_TOKEN", "").strip()
TURN_WEBHOOK_SECRET: str = os.getenv("TURN_WEBHOOK_SECRET", "").strip()
TURN_MESSAGES_URL: str = "https://whatsapp.turn.io/v1/messages"

app = FastAPI(title="Turn.io webhook echo server")


def _signature_valid(raw_body: bytes, signature: str | None) -> bool | None:
    """
    Verify Turn's HMAC-SHA256 webhook signature.

    Args:
        raw_body: Raw request body bytes.
        signature: Value of the ``X-Turn-Hook-Signature`` header.

    Returns:
        True/False when verification was possible, None when secret or
        signature is missing (verification skipped).
    """
    if not TURN_WEBHOOK_SECRET or not signature:
        return None
    digest = hmac.new(TURN_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _send_reply(to_wa_id: str, inbound_text: str) -> None:
    """
    Send a simple outbound text message via Turn's messages API.

    Args:
        to_wa_id: Recipient WhatsApp id (digits, no plus sign).
        inbound_text: The inbound text being echoed (for context in reply).

    Raises:
        httpx.HTTPStatusError: If Turn rejects the outbound send.
    """
    headers = {
        "Authorization": f"Bearer {TURN_API_TOKEN}",
        "Accept": "application/vnd.v1+json",
        "Content-Type": "application/json",
    }
    body = {
        "to": to_wa_id,
        "type": "text",
        "text": {"body": f"Echo server received: {inbound_text!r} — relay works!"},
    }
    response = httpx.post(TURN_MESSAGES_URL, headers=headers, json=body, timeout=20.0)
    response.raise_for_status()
    logger.info("OUTBOUND sent to %s: HTTP %s %s", to_wa_id, response.status_code, response.text[:150])


@app.post("/")
@app.post("/webhook")
@app.post("/v1/webhooks/whatsapp")
async def turn_webhook(
    request: Request,
    x_turn_hook_subscription: str | None = Header(default=None, alias="X-Turn-Hook-Subscription"),
    x_turn_hook_signature: str | None = Header(default=None, alias="X-Turn-Hook-Signature"),
) -> dict[str, Any]:
    """
    Handle a Turn webhook event and echo-reply to inbound contact messages.

    Args:
        request: Incoming HTTP request.
        x_turn_hook_subscription: Turn subscription type header.
        x_turn_hook_signature: Turn HMAC signature header.

    Returns:
        Simple acknowledgement JSON (Turn only requires a fast 2xx).
    """
    raw = await request.body()
    sig_ok = _signature_valid(raw, x_turn_hook_signature)

    try:
        payload: dict[str, Any] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        logger.warning("Received non-JSON body (%d bytes)", len(raw))
        return {"ack": True, "note": "non-json body"}

    logger.info(
        "WEBHOOK sub=%s sig=%s keys=%s",
        x_turn_hook_subscription or "(none)",
        {True: "valid", False: "INVALID", None: "skipped"}[sig_ok],
        sorted(payload.keys()),
    )

    # Outbound delivery statuses (sent/delivered/read) — log only.
    statuses = payload.get("statuses")
    if isinstance(statuses, list) and statuses:
        for status in statuses:
            logger.info("STATUS %s -> %s", status.get("id", "?"), status.get("status", "?"))
        return {"ack": True, "kind": "statuses"}

    # Inbound contact messages (whatsapp subscription).
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        for message in messages:
            direction = (
                (message.get("_vnd") or {}).get("v1", {}).get("direction", "inbound")
            )
            sender = str(message.get("from", "")).strip()
            text = (message.get("text") or {}).get("body", f"<{message.get('type')}>")

            if direction == "outbound":
                logger.info("OUTBOUND ECHO (ours): %r — not replying", text)
                continue

            logger.info("INBOUND from=%s type=%s text=%r", sender, message.get("type"), text)
            if sender:
                try:
                    _send_reply(sender, str(text))
                except httpx.HTTPError as exc:
                    logger.error("Reply failed: %s", exc)
        return {"ack": True, "kind": "messages"}

    logger.info("Unrecognized payload shape — acked")
    return {"ack": True, "kind": "other"}


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}

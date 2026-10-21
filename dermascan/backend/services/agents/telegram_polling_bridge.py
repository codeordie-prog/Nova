"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Local Telegram polling bridge.

Polls Telegram Bot API using getUpdates and forwards each update payload
into the local backend Telegram webhook endpoint.

This is just for local host testing, in production we will have to register a webhook
to our BASE URL then use that as the endpoint not this module.
"""


import os
import time
from typing import Any

import requests

from dermascan.shared.load_env import bootstrap_env
from dermascan.shared.logger import get_logger

bootstrap_env()

logger = get_logger("agents")

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_LOCAL_WEBHOOK_URL: str = os.getenv(
    "TELEGRAM_LOCAL_WEBHOOK_URL",
    "http://127.0.0.1:8088/v1/webhooks/telegram",
)
TELEGRAM_POLL_TIMEOUT_S: int = int(os.getenv("TELEGRAM_POLL_TIMEOUT_S", "30"))
TELEGRAM_POLL_LIMIT: int = int(os.getenv("TELEGRAM_POLL_LIMIT", "20"))
TELEGRAM_HTTP_TIMEOUT_S: int = int(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "40"))
TELEGRAM_RETRY_DELAY_S: float = float(os.getenv("TELEGRAM_RETRY_DELAY_S", "2"))


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _disable_webhook_for_polling() -> None:
    resp = requests.post(
        _api_url("deleteWebhook"),
        json={"drop_pending_updates": False},
        timeout=TELEGRAM_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"deleteWebhook failed: {data}")


def _get_updates(offset: int | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": TELEGRAM_POLL_TIMEOUT_S,
        "limit": TELEGRAM_POLL_LIMIT,
        "allowed_updates": ["message", "edited_message"],
    }
    if offset is not None:
        payload["offset"] = offset

    resp = requests.post(
        _api_url("getUpdates"),
        json=payload,
        timeout=TELEGRAM_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    result = data.get("result") or []
    return [item for item in result if isinstance(item, dict)]


def _forward_update(update: dict[str, Any]) -> None:
    local_payload: dict[str, Any] = {}
    message = update.get("message")
    edited_message = update.get("edited_message")
    if isinstance(message, dict) and message:
        local_payload["message"] = message
    elif isinstance(edited_message, dict) and edited_message:
        local_payload["edited_message"] = edited_message
    else:
        return

    resp = requests.post(
        TELEGRAM_LOCAL_WEBHOOK_URL,
        json=local_payload,
        timeout=TELEGRAM_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()


def run_bridge_forever() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    logger.info("starting telegram polling bridge -> %s", TELEGRAM_LOCAL_WEBHOOK_URL)
    _disable_webhook_for_polling()

    next_offset: int | None = None
    while True:
        try:
            updates = _get_updates(next_offset)
            for update in updates:
                update_id = int(update.get("update_id") or 0)
                if update_id <= 0:
                    continue
                _forward_update(update)
                next_offset = update_id + 1
        except KeyboardInterrupt:
            logger.info("telegram polling bridge stopped")
            break
        except Exception:
            logger.exception("telegram polling bridge iteration failed")
            time.sleep(TELEGRAM_RETRY_DELAY_S)


if __name__ == "__main__":
    run_bridge_forever()

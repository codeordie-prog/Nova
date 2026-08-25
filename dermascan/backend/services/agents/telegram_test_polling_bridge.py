"""
Local Telegram polling bridge for the **test bot only**.

Uses ``TELEGRAM_TEST_BOT_TOKEN`` — never ``TELEGRAM_BOT_TOKEN`` — so production
webhook/polling on the live bot is untouched.

Polls ``getUpdates`` for the test bot and forwards payloads to the local API
webhook (``TELEGRAM_LOCAL_WEBHOOK_URL``).

For replies to come from the same test bot, set ``TELEGRAM_BOT_TOKEN`` to the
same test token in ``.env`` while running ``do_work.py`` locally (cloud keeps
its own production token).
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from dermascan.shared.load_env import bootstrap_env
from dermascan.shared.logger import get_logger

bootstrap_env()

logger = get_logger("agents")

TELEGRAM_TEST_BOT_TOKEN: str = os.getenv("TELEGRAM_TEST_BOT_TOKEN", "").strip()
TELEGRAM_LOCAL_WEBHOOK_URL: str = os.getenv(
    "TELEGRAM_LOCAL_WEBHOOK_URL",
    "http://127.0.0.1:8088/v1/webhooks/telegram",
)
TELEGRAM_POLL_TIMEOUT_S: int = int(os.getenv("TELEGRAM_POLL_TIMEOUT_S", "30"))
TELEGRAM_POLL_LIMIT: int = int(os.getenv("TELEGRAM_POLL_LIMIT", "20"))
TELEGRAM_HTTP_TIMEOUT_S: int = int(os.getenv("TELEGRAM_HTTP_TIMEOUT_S", "40"))
TELEGRAM_RETRY_DELAY_S: float = float(os.getenv("TELEGRAM_RETRY_DELAY_S", "2"))


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TEST_BOT_TOKEN}/{method}"


def _log_test_bot_identity() -> None:
    """
    Log the test bot username at startup so the operator opens the right chat.

    Raises:
        RuntimeError: When ``getMe`` fails or the token is invalid.
    """
    resp = requests.get(_api_url("getMe"), timeout=TELEGRAM_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getMe failed: {data}")
    result = data.get("result") or {}
    username = str(result.get("username") or "")
    bot_id = result.get("id")
    first_name = str(result.get("first_name") or "")
    logger.info(
        "test bot identity: id=%s username=@%s name=%s",
        bot_id,
        username,
        first_name,
    )
    if username:
        logger.info("message this bot in Telegram: https://t.me/%s", username)


def _disable_webhook_for_polling() -> None:
    """Remove webhook on the **test** bot only (required before getUpdates)."""
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
    """
    Poll the test bot and forward updates to the local ingress webhook.

    Raises:
        RuntimeError: When ``TELEGRAM_TEST_BOT_TOKEN`` is missing or invalid.
    """
    if not TELEGRAM_TEST_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_TEST_BOT_TOKEN is required for the test polling bridge. "
            "Do not use TELEGRAM_BOT_TOKEN here — that keeps production separate."
        )

    logger.info(
        "starting telegram TEST polling bridge -> %s (token suffix=...%s)",
        TELEGRAM_LOCAL_WEBHOOK_URL,
        TELEGRAM_TEST_BOT_TOKEN[-8:] if len(TELEGRAM_TEST_BOT_TOKEN) >= 8 else "????",
    )
    _log_test_bot_identity()
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
            logger.info("telegram test polling bridge stopped")
            break
        except Exception:
            logger.exception("telegram test polling bridge iteration failed")
            time.sleep(TELEGRAM_RETRY_DELAY_S)


if __name__ == "__main__":
    run_bridge_forever()

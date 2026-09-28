"""Register Nova ingress as Turn primary application webhook."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dermascan.shared.load_env import bootstrap_env

_TURN_SETTINGS_URL = "https://whatsapp.turn.io/v1/settings/application"


def _token_account(token: str) -> str:
    """Return Turn JWT ``sub`` claim (e.g. ``number:8801``) without logging the token."""
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return str(payload.get("sub", "(unknown)"))
    except (IndexError, json.JSONDecodeError, ValueError):
        return "(could not decode)"


def register_webhook() -> int:
    """
    PATCH Turn primary webhook to ngrok → Nova ingress path.

    Returns:
        int: Exit code (0 success, 1 failure).
    """
    bootstrap_env()

    token = os.getenv("TURN_API_TOKEN", "").strip()
    ngrok = os.getenv("TURN_NGROK_BASE_URL", "").strip().rstrip("/")
    entry = os.getenv("TURN_SANDBOX_ENTRY_CODE", "").strip()

    if not token:
        print("ERROR: Set TURN_API_TOKEN in .env (token from the Turn account you are using).")
        return 1
    if not ngrok:
        print("ERROR: Set TURN_NGROK_BASE_URL in .env (e.g. https://xxx.ngrok-free.dev).")
        return 1

    webhook_url = f"{ngrok}/v1/webhooks/whatsapp"
    # Settings API requires Accept: application/json (not vnd) or GET returns url: null.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"webhooks": {"url": webhook_url}}

    print(f"Token account (JWT sub): {_token_account(token)}")
    print(f"Sandbox entry code (from .env): {entry or '(not set)'}")
    print(f"Registering primary webhook: {webhook_url}")
    print()

    patch_resp = httpx.patch(_TURN_SETTINGS_URL, headers=headers, json=body, timeout=30.0)
    print(f"PATCH HTTP {patch_resp.status_code}")
    if patch_resp.status_code >= 400:
        print(patch_resp.text[:500])
        return 1

    get_resp = httpx.get(_TURN_SETTINGS_URL, headers=headers, timeout=30.0)
    print(f"GET  HTTP {get_resp.status_code}")
    registered = (
        get_resp.json()
        .get("settings", {})
        .get("application", {})
        .get("webhooks", {})
        .get("url", "")
    )
    print(f"Registered URL: {registered}")
    print(f"Match: {'YES' if registered == webhook_url else 'NO'}")
    return 0 if registered == webhook_url else 2


if __name__ == "__main__":
    raise SystemExit(register_webhook())

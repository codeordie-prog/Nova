"""Verify Turn primary webhook registration and ngrok → Nova relay path."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dermascan.shared.load_env import bootstrap_env


def _sign(body: bytes, secret: str) -> str:
    """Compute Turn webhook HMAC-SHA256 signature (base64)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def main() -> int:
    """Run webhook verification checks."""
    bootstrap_env()

    token = os.getenv("TURN_API_TOKEN", "").strip()
    ngrok = os.getenv("TURN_NGROK_BASE_URL", "").strip().rstrip("/")
    secret = os.getenv("TURN_WEBHOOK_SECRET", "").strip()
    expected = f"{ngrok}/v1/webhooks/whatsapp"

    if not token:
        print("ERROR: TURN_API_TOKEN not set in .env")
        return 1
    if not ngrok:
        print("ERROR: TURN_NGROK_BASE_URL not set in .env")
        return 1

    print("=== 1. Turn primary webhook (GET /v1/settings/application) ===")
    settings_resp = httpx.get(
        "https://whatsapp.turn.io/v1/settings/application",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=20.0,
    )
    print(f"HTTP {settings_resp.status_code}")
    registered = (
        settings_resp.json()
        .get("settings", {})
        .get("application", {})
        .get("webhooks", {})
        .get("url", "")
    )
    print(f"Registered : {registered or '(none)'}")
    print(f"Expected   : {expected}")
    print(f"Match      : {'YES' if registered == expected else 'NO'}")

    print("\n=== 2. Turn UI webhooks (GET /v1/webhooks) ===")
    print("Per Turn docs, each UI webhook must enable the `whatsapp` subscription")
    print("(inbound messages). Subscriptions are not exposed by this API — check Turn UI.")
    ui_resp = httpx.get(
        "https://whatsapp.turn.io/v1/webhooks",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.v1+json",
        },
        timeout=20.0,
    )
    print(f"HTTP {ui_resp.status_code}")
    ui_webhooks = ui_resp.json().get("webhooks", []) if ui_resp.status_code == 200 else []
    if not ui_webhooks:
        print("  (none listed — add one in Turn UI → Settings → API & Webhooks)")
    for wh in ui_webhooks:
        url = wh.get("url", "")
        ok_path = url == expected
        print(f"  - {url}")
        print(f"    path ok: {'YES' if ok_path else 'NO — fix or delete in Turn UI'}")

    print("\n=== 3. ngrok → Nova (empty payload, no auth) ===")
    empty_resp = httpx.post(
        f"{ngrok}/v1/webhooks/whatsapp",
        json={"messages": []},
        timeout=20.0,
    )
    print(f"HTTP {empty_resp.status_code}  body={empty_resp.text[:120]}")

    print("\n=== 4. Turn-shaped signed POST via ngrok ===")
    payload = {
        "contacts": [{"profile": {"name": "Relay Test"}, "wa_id": "254745840597"}],
        "messages": [
            {
                "from": "254745840597",
                "id": "wamid.turn-relay-probe",
                "timestamp": "1726840800",
                "type": "text",
                "text": {"body": "turn relay probe"},
            }
        ],
    }
    body_bytes = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Turn-Hook-Subscription": "whatsapp",
    }
    if secret:
        headers["X-Turn-Hook-Signature"] = _sign(body_bytes, secret)
    else:
        print("(TURN_WEBHOOK_SECRET unset — skipping signature)")

    signed_resp = httpx.post(
        f"{ngrok}/v1/webhooks/whatsapp",
        content=body_bytes,
        headers=headers,
        timeout=20.0,
    )
    print(f"HTTP {signed_resp.status_code}  body={signed_resp.text[:200]}")

    print("\n=== Summary ===")
    ok_settings = settings_resp.status_code == 200 and registered == expected
    ok_path = empty_resp.status_code == 200
    ok_signed = signed_resp.status_code == 200 and "queued" in signed_resp.text
    print(f"Webhook registered correctly : {'YES' if ok_settings else 'NO'}")
    print(f"ngrok path works             : {'YES' if ok_path else 'NO'}")
    print(f"Signed Turn payload accepted : {'YES' if ok_signed else 'NO'}")
    print(
        "\nNote: This proves Turn *can* reach Nova if it POSTs inbound messages."
        "\nReal WhatsApp inbound still requires a user message on the sandbox line."
    )
    return 0 if (ok_settings and ok_path and ok_signed) else 2


if __name__ == "__main__":
    raise SystemExit(main())

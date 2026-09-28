"""Check whether Turn recorded an inbound message for a sandbox contact."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dermascan.shared.load_env import bootstrap_env


def main() -> int:
    """Probe Turn contact profile and inbox for inbound activity."""
    bootstrap_env()
    token = os.getenv("TURN_API_TOKEN", "").strip()
    wa_id = os.getenv("TURN_SANDBOX_TEST_TO", "254745840597").strip()
    if not token:
        print("ERROR: TURN_API_TOKEN missing")
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.v1+json",
    }
    base = "https://whatsapp.turn.io"

    print(f"Checking contact: {wa_id}")
    print()

    profile_resp = httpx.get(f"{base}/v1/contacts/{wa_id}/profile", headers=headers, timeout=20)
    print(f"Profile HTTP {profile_resp.status_code}")
    if profile_resp.status_code == 200:
        fields = profile_resp.json().get("fields", {})
        print(f"  first_message_received_at : {fields.get('first_message_received_at')}")
        print(f"  last_message_received_at  : {fields.get('last_message_received_at')}")
        print(f"  last_seen_at              : {fields.get('last_seen_at')}")
    else:
        print(f"  body: {profile_resp.text[:300]}")

    print()
    inbox_resp = httpx.get(f"{base}/v1/contacts/{wa_id}/messages", headers=headers, timeout=20)
    print(f"Inbox HTTP {inbox_resp.status_code}")
    if inbox_resp.status_code == 200:
        payload = inbox_resp.json()
        messages = payload.get("messages") or []
        print(f"  message count: {len(messages)}")
        for msg in messages[:3]:
            direction = (msg.get("_vnd") or {}).get("v1", {}).get("direction", "?")
            body = (msg.get("text") or {}).get("body") or msg.get("type")
            print(f"  - {direction}: {body}")
    else:
        print(f"  body: {inbox_resp.text[:300]}")

    print()
    settings_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    settings = httpx.get(
        f"{base}/v1/settings/application", headers=settings_headers, timeout=20
    )
    webhook_url = (
        settings.json()
        .get("settings", {})
        .get("application", {})
        .get("webhooks", {})
        .get("url", "")
    )
    print(f"Primary webhook: {webhook_url}")

    first = None
    if profile_resp.status_code == 200:
        first = profile_resp.json().get("fields", {}).get("first_message_received_at")

    print()
    if first:
        print("VERDICT: Turn recorded inbound — webhook should have fired.")
    else:
        print("VERDICT: Turn did NOT record inbound — problem is on Turn sandbox side.")
        print("WhatsApp delivery (double ticks) != Turn receiving the message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

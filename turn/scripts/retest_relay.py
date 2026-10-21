"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Re-register webhook, send ping, print status."""


from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
from dermascan.shared.load_env import bootstrap_env


def main() -> None:
    bootstrap_env()
    token = os.getenv("TURN_API_TOKEN", "").strip()
    to = os.getenv("TURN_SANDBOX_TEST_TO", "254765455121")
    ngrok = os.getenv("TURN_NGROK_BASE_URL", "").strip().rstrip("/")
    webhook = f"{ngrok}/v1/webhooks/whatsapp"
    app_h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    pr = httpx.patch(
        "https://whatsapp.turn.io/v1/settings/application",
        headers=app_h,
        json={"webhooks": {"url": webhook}},
        timeout=30.0,
    )
    gr = httpx.get(
        "https://whatsapp.turn.io/v1/settings/application", headers=app_h, timeout=30.0
    )
    primary = (
        gr.json()
        .get("settings", {})
        .get("application", {})
        .get("webhooks", {})
        .get("url")
    )
    print(f"Primary webhook: {primary!r} (PATCH {pr.status_code})")

    vnd = {**app_h, "Accept": "application/vnd.v1+json"}
    wh = httpx.get("https://whatsapp.turn.io/v1/webhooks", headers=vnd, timeout=30.0)
    print(f"UI webhooks: {wh.json().get('webhooks', [])}")

    sr = httpx.post(
        "https://whatsapp.turn.io/v1/messages",
        headers=vnd,
        json={
            "to": to,
            "type": "text",
            "text": {"body": "Relay retest - reply hello now (VPN off both sides)"},
        },
        timeout=30.0,
    )
    print(f"Outbound ping: HTTP {sr.status_code}")
    print(sr.text[:200])

    probe = httpx.post(
        f"{webhook}",
        json={
            "messages": [
                {
                    "from": to,
                    "id": "wamid.relay-retest-probe",
                    "type": "text",
                    "text": {"body": "simulated inbound"},
                }
            ]
        },
        timeout=20.0,
    )
    print(f"Nova simulate: HTTP {probe.status_code} {probe.text[:120]}")


if __name__ == "__main__":
    main()

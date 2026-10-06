"""Inspect the Turn.io ``Qutis`` journey (metadata + triggers)."""

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

_BASE = "https://whatsapp.turn.io"


def main() -> int:
    bootstrap_env()
    token = os.getenv("TURN_API_TOKEN", "").strip()
    if not token:
        print("ERROR: TURN_API_TOKEN not set")
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    stacks_resp = httpx.get(f"{_BASE}/v1/stacks", headers=headers, timeout=30.0)
    print(f"GET /v1/stacks HTTP {stacks_resp.status_code}")
    if stacks_resp.status_code != 200:
        print(stacks_resp.text[:500])
        return 1

    items = stacks_resp.json()
    if isinstance(items, dict):
        items = items.get("stacks") or items.get("data") or []

    qutis = next(
        (s for s in items if str(s.get("name", "")).strip().lower() == "qutis"),
        None,
    )
    if not qutis:
        print("No journey named 'Qutis'. Available:", [s.get("name") for s in items])
        return 1

    uuid = str(qutis["uuid"])
    print("\n=== Qutis (list entry) ===")
    print(json.dumps(qutis, indent=2))

    detail_resp = httpx.get(f"{_BASE}/v1/stacks/{uuid}", headers=headers, timeout=30.0)
    print(f"\nGET /v1/stacks/{uuid} HTTP {detail_resp.status_code}")
    if detail_resp.status_code == 200:
        print(json.dumps(detail_resp.json(), indent=2)[:12000])
    else:
        print(detail_resp.text[:800])

    for extra in ("triggers", "notebook", "code"):
        path = f"{_BASE}/v1/stacks/{uuid}/{extra}"
        resp = httpx.get(path, headers=headers, timeout=30.0)
        print(f"\nGET .../{extra} HTTP {resp.status_code}")
        if resp.status_code == 200:
            try:
                payload = resp.json()
                print(json.dumps(payload, indent=2)[:12000])
            except json.JSONDecodeError:
                print(resp.text[:12000])
        elif resp.status_code != 404:
            print(resp.text[:400])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

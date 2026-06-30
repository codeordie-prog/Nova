"""One-off probe for the qutis WhatsApp sidecar on WHATSAPP_API_BASE_URL."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_secret() -> str:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        env_path = Path(__file__).resolve().parents[3] / ".env"
    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"^INTERNAL_API_SECRET=(.+)$", text, re.M)
    if not match:
        raise RuntimeError("INTERNAL_API_SECRET not found in .env")
    return match.group(1).strip()


def _probe(base_url: str, label: str, payload: dict[str, str] | None, secret: str | None) -> None:
    print(f"=== {label} ===")
    if payload is None:
        with urllib.request.urlopen(f"{base_url}/", timeout=10) as response:
            print(f"Status: {response.status}")
            print(response.read().decode())
        print()
        return

    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Internal-Secret"] = secret
    request = urllib.request.Request(
        f"{base_url}/send",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
            print(f"Status: {response.status}")
            print(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"Status: {exc.code}")
        print(body or "(empty body)")
    print()


def main() -> int:
    base_url = os.getenv("WHATSAPP_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    secret = _load_secret()

    _probe(base_url, "GET /", None, None)
    _probe(
        base_url,
        "POST /send fake E2E number",
        {"to": "254700000001", "kind": "text", "text": "e2e fake probe"},
        secret,
    )
    _probe(
        base_url,
        "POST /send real number (254702880704)",
        {"to": "254702880704", "kind": "text", "text": "terminal real probe"},
        secret,
    )
    _probe(
        base_url,
        "POST /send no secret",
        {"to": "254702880704", "kind": "text", "text": "no secret"},
        None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

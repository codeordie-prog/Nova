"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-24
License: MIT

Fetch inbound WhatsApp media from Turn.io for Nova ingress.

Meta CDN URLs in Turn webhooks (``lookaside.fbsbx.com``) are not publicly
fetchable; use ``GET /v1/media/{id}`` with a Turn bearer token instead.
"""


from __future__ import annotations

import base64
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx

_LOGGER: logging.Logger = logging.getLogger("dermascan.whatsapp_turn_media")

TURN_API_BASE_URL: str = os.getenv("TURN_API_BASE_URL", "https://whatsapp.turn.io").rstrip("/")
TURN_API_TOKEN: str = os.getenv("TURN_API_TOKEN", "").strip()
WHATSAPP_MEDIA_HTTP_TIMEOUT_S: float = float(os.getenv("WHATSAPP_MEDIA_HTTP_TIMEOUT_S", "30"))

_TURN_ACCEPT: str = "application/vnd.v1+json"
_META_CDN_MARKERS: tuple[str, ...] = (
    "lookaside.fbsbx.com",
    "fbcdn.net",
    "facebook.com",
)


def is_whatsapp_protected_media_url(url: str) -> bool:
    """
    Return True when a URL requires WhatsApp Cloud API auth to download.

    Args:
        url: Candidate media URL from a webhook payload.

    Returns:
        bool: True for Meta lookaside / CDN hosts.
    """
    normalized = str(url or "").strip()
    if not normalized.lower().startswith(("http://", "https://")):
        return False
    host = urlparse(normalized).netloc.lower()
    return any(marker in host for marker in _META_CDN_MARKERS)


def fetch_turn_media_bytes(media_id: str) -> tuple[bytes, str]:
    """
    Download WhatsApp media bytes via Turn's Media API.

    Args:
        media_id: ``image.id`` (or document id) from the Turn/Meta webhook.

    Returns:
        Tuple of raw bytes and MIME type string.

    Raises:
        RuntimeError: When token is missing or Turn returns an error.
        ValueError: When ``media_id`` is empty.
    """
    media_key = str(media_id or "").strip()
    if not media_key:
        raise ValueError("missing whatsapp media id")
    if not TURN_API_TOKEN:
        raise RuntimeError("TURN_API_TOKEN is not configured; cannot fetch WhatsApp media")

    headers = {
        "Authorization": f"Bearer {TURN_API_TOKEN}",
        "Accept": _TURN_ACCEPT,
    }
    url = f"{TURN_API_BASE_URL}/v1/media/{media_key}"

    with httpx.Client(timeout=WHATSAPP_MEDIA_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Turn media GET failed: HTTP {response.status_code} body={response.text[:200]}"
            )

        content_type = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type.startswith("application/json") or content_type.endswith("+json"):
            payload: dict[str, Any] = response.json()
            nested_url = str(payload.get("url") or payload.get("link") or "").strip()
            mime_type = str(payload.get("mime_type") or payload.get("mimetype") or "image/jpeg")
            if nested_url:
                nested = client.get(nested_url, headers=headers)
                if nested.status_code >= 400:
                    raise RuntimeError(f"Turn media nested GET failed: HTTP {nested.status_code}")
                nested_ct = str(nested.headers.get("content-type") or mime_type).split(";")[0].strip()
                return nested.content, nested_ct or mime_type
            raw = payload.get("data") or payload.get("file")
            if isinstance(raw, str):
                return base64.b64decode(raw), mime_type
            raise RuntimeError(f"Turn media JSON missing url/data: keys={list(payload.keys())}")

        mime_type = content_type if content_type else "image/jpeg"
        if not response.content:
            raise RuntimeError("Turn media GET returned empty body")
        return response.content, mime_type

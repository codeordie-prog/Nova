"""
Author: Dermascan Team
Date: 11-20-2026
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Bearer authentication for external agent clients (OpenClaw, MCP, etc.)."""


from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

_AGENT_API_KEY_ENV: str = "QUTIS_AGENT_API_KEY"


def agent_api_key_configured() -> bool:
    """
    Return True when ``QUTIS_AGENT_API_KEY`` is set to a non-empty value.

    Returns:
        bool: True if agent API auth is required.
    """
    return bool(os.getenv(_AGENT_API_KEY_ENV, "").strip())


def verify_agent_api_bearer(request: Request) -> None:
    """
    Authorize ``POST/GET /v1/agents/*`` using ``Authorization: Bearer``.

    When ``QUTIS_AGENT_API_KEY`` is unset, auth is skipped (local dev only).

    Args:
        request: Incoming HTTP request.

    Raises:
        HTTPException: 401 when the key is required and missing or wrong.
    """
    expected = os.getenv(_AGENT_API_KEY_ENV, "").strip()
    if not expected:
        return

    auth_header = str(request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")

    supplied = auth_header[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid agent API key")
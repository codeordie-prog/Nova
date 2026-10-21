"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Load scenario: ingress-only admission control (no inference).

Concerns: S1 — admission caps input; L1/L2 — RPS ceiling and 429 onset.
Requires live FastAPI + Redis; inference layer mocked or bypassed.
"""


from __future__ import annotations

# Scaffold — implement as scripted Locust UserGroup or httpx burst driver.


def run_ingress_only_profile() -> None:
    """Ramp ingress RPS until error-rate > 1%; document 429 onset (L2)."""
    raise NotImplementedError("Scaffold: implementation pending")

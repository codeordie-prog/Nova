"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Load scenario: mixed text + image + media-group bursts.

Concerns: C6 — media-group batching under load; L3 — p95/p99 latency curve.
Requires live stack + sample images.
"""


from __future__ import annotations

# Scaffold — implement as Locust task mix or ThreadPool burst driver.


def run_mixed_media_profile() -> None:
    """Burst albums + single images; measure batching and latency (L3)."""
    raise NotImplementedError("Scaffold: implementation pending")

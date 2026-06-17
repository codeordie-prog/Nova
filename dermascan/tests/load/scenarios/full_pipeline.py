"""Load scenario: full pipeline ingress → worker → outbound.

Concerns: S5 — throughput vs concurrency curve; S6 — horizontal worker scaling.
Requires live stack with mock inference layer.
"""

from __future__ import annotations

# Scaffold — implement as Locust task set or async httpx soak driver.


def run_full_pipeline_profile() -> None:
    """Ramp 1→N concurrent; measure throughput plateau and p95/p99 (S5)."""
    raise NotImplementedError("Scaffold: implementation pending")

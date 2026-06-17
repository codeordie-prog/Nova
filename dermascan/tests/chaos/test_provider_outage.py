"""Chaos tests: provider outage (Qwen / MedGemma).

Concerns: R7 — provider failover via circuit breaker.
Fault injection via monkeypatch of provider clients.
"""

from __future__ import annotations

import pytest


@pytest.mark.chaos
class TestProviderOutage:
    """Provider down → circuit opens, failover path engaged."""

    def test_qwen_outage_opens_circuit(self) -> None:
        """R7: 3 consecutive Qwen failures → circuit open, allow() False."""
        pytest.skip("Scaffold: implementation pending")

    def test_medgemma_timeout_triggers_circuit(self) -> None:
        """L8: MedGemma cold-start/timeout under load → circuit response documented."""
        pytest.skip("Scaffold: implementation pending")

    def test_half_open_recovery_after_cooldown(self) -> None:
        """R7: After cooldown, probe succeeds → circuit closes, traffic resumes."""
        pytest.skip("Scaffold: implementation pending")

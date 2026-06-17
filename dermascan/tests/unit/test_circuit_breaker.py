"""Unit tests for provider circuit breaker state machine.

Concerns: R7 — provider failover; C5 — circuit-breaker thread safety.
Source: provider_circuit_breaker.py
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestProviderCircuitBreaker:
    """closed → open → half_open → closed transitions."""

    def test_opens_after_consecutive_failures(self) -> None:
        """R7: N consecutive provider failures → circuit opens, allow() returns False."""
        pytest.skip("Scaffold: implementation pending")

    def test_half_open_probe_after_cooldown(self) -> None:
        """R7: After cooldown, half-open allows a single probe request."""
        pytest.skip("Scaffold: implementation pending")

    def test_closes_on_success_in_half_open(self) -> None:
        """R7: Successful probe in half-open → circuit closes."""
        pytest.skip("Scaffold: implementation pending")

    def test_reopens_on_half_open_failure(self) -> None:
        """R7: Failed probe in half-open → circuit reopens."""
        pytest.skip("Scaffold: implementation pending")

    def test_concurrent_state_transitions_consistent(self) -> None:
        """C5: Concurrent record_failure/success → no torn reads or invalid states."""
        pytest.skip("Scaffold: implementation pending")

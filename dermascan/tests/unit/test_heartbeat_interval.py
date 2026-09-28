"""Tests for stream heartbeat interval computation."""

from __future__ import annotations

from dermascan.backend.services.agents.heartbeat import (
    compute_heartbeat_interval_sec,
)


def test_interval_below_idle_and_lease() -> None:
    interval = compute_heartbeat_interval_sec(300_000, 180)
    # Must stay under both deadlines so idle never crosses the reclaim threshold.
    assert interval <= 300.0 / 3.0
    assert interval <= 180.0 / 3.0
    assert interval >= 5.0


def test_interval_respects_floor_on_tiny_idle() -> None:
    assert compute_heartbeat_interval_sec(1_000, None) == 5.0


def test_interval_without_lease_uses_idle_only() -> None:
    assert compute_heartbeat_interval_sec(300_000, None) == 100.0

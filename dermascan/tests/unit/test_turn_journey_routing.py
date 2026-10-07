"""Unit tests for Turn Main Menu ingress routing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dermascan.shared import turn_journey_routing as routing


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dermascan", True),
        ("Dermascan", True),
        ("", False),
        (None, False),
        ("platform", False),
    ],
)
def test_is_dermascan_active_handler(value: object, expected: bool) -> None:
    assert routing.is_dermascan_active_handler(value) is expected


def test_turn_main_menu_routing_enabled_requires_uuid_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TURN_MAIN_MENU_JOURNEY_UUID", raising=False)
    monkeypatch.delenv("TURN_API_TOKEN", raising=False)
    assert routing.turn_main_menu_routing_enabled() is False

    monkeypatch.setenv("TURN_MAIN_MENU_JOURNEY_UUID", "298e2cdc-c374-4db7-a079-6b98cd4ce825")
    monkeypatch.setenv("TURN_API_TOKEN", "token")
    assert routing.turn_main_menu_routing_enabled() is True


def test_is_turn_stack_claim_active() -> None:
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert routing.is_turn_stack_claim_active({"scope": "stack", "claimed_until": future}) is True
    assert routing.is_turn_stack_claim_active({"scope": "stack", "claimed_until": past}) is False
    assert routing.is_turn_stack_claim_active({"scope": "webhook"}) is False
    assert routing.is_turn_stack_claim_active(None) is False

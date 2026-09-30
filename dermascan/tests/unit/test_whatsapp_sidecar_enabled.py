"""Tests for workers Turn sidecar enablement."""

from __future__ import annotations

import pytest

from deploy.scripts.run_workers_with_turn_sidecar import whatsapp_sidecar_enabled


def test_sidecar_off_when_explicitly_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_SIDECAR_ENABLED", "false")
    monkeypatch.setenv("TURN_API_TOKEN", "token")
    assert whatsapp_sidecar_enabled() is False


def test_sidecar_on_when_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURN_API_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_SIDECAR_ENABLED", "true")
    assert whatsapp_sidecar_enabled() is True


def test_sidecar_defaults_on_when_turn_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_SIDECAR_ENABLED", raising=False)
    monkeypatch.setenv("TURN_API_TOKEN", "sandbox-token")
    assert whatsapp_sidecar_enabled() is True


def test_sidecar_defaults_off_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_SIDECAR_ENABLED", raising=False)
    monkeypatch.delenv("TURN_API_TOKEN", raising=False)
    assert whatsapp_sidecar_enabled() is False

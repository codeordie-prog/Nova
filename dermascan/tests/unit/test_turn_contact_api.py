"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for synchronous Turn contact / journey API helpers."""


from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from dermascan.shared import turn_contact_api as api


def test_normalize_turn_wa_id() -> None:
    assert api.normalize_turn_wa_id("+254712345678") == "254712345678"
    assert api.normalize_turn_wa_id("254712345678") == "254712345678"


def test_mydawa_menu_exit_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_MYDAWA_MENU_EXIT_ENABLED", "false")
    assert api.mydawa_menu_exit_enabled() is False
    monkeypatch.setenv("TURN_MYDAWA_MENU_EXIT_ENABLED", "true")
    assert api.mydawa_menu_exit_enabled() is True


def test_clear_turn_active_handler_sync_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_API_TOKEN", "token")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = ""
    with patch.object(api.httpx, "patch", return_value=mock_response) as mock_patch:
        ok, detail = api.clear_turn_active_handler_sync("254712345678")
    assert ok is True
    assert detail == "ok"
    mock_patch.assert_called_once()


def test_return_to_mydawa_main_menu_sync_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_MYDAWA_MENU_EXIT_ENABLED", "true")
    monkeypatch.setenv("TURN_API_TOKEN", "token")
    monkeypatch.setenv("TURN_MAIN_MENU_JOURNEY_UUID", "298e2cdc-c374-4db7-a079-6b98cd4ce825")

    patch_ok = MagicMock(status_code=200, text="")
    start_ok = MagicMock(status_code=201, text='{"success": true}')

    with patch.object(api.httpx, "patch", return_value=patch_ok):
        with patch.object(api.httpx, "post", return_value=start_ok):
            result = api.return_to_mydawa_main_menu_sync(
                wa_id="254712345678",
                message_id="wamid.test",
            )

    assert result["status"] == "ok"


def test_return_to_mydawa_main_menu_sync_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_MYDAWA_MENU_EXIT_ENABLED", "false")
    result = api.return_to_mydawa_main_menu_sync(wa_id="254712345678")
    assert result["status"] == "disabled"

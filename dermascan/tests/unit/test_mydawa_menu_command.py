"""Unit tests for MYDAWA /menu worker shortcut helpers."""

from __future__ import annotations

from dermascan.backend.services.agents import orchestrator_worker as worker


def test_ingress_payload_text_extracts_message() -> None:
    payload = '{"content": {"text": "/menu"}}'
    assert worker._ingress_payload_text(payload) == "/menu"


def test_mydawa_menu_command_constant_default() -> None:
    assert worker.MYDAWA_MENU_COMMAND == "/menu"

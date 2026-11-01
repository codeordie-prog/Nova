"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for reminder feature flags."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_flags_module():
    module_path = Path(__file__).resolve().parents[2] / "reminders" / "flags.py"
    spec = importlib.util.spec_from_file_location(
        "dermascan_reminders_flags_under_test",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load reminders flags from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reminders_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMINDERS_ENABLED", raising=False)
    flags = _load_flags_module()
    assert flags.reminders_enabled() is False


def test_default_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMINDERS_DEFAULT_TIMEZONE", raising=False)
    flags = _load_flags_module()
    assert flags.default_timezone() == "Africa/Nairobi"

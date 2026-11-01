"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for routine_key slugging."""

from __future__ import annotations

from dermascan.reminders.routine_key import routine_key_from_label


def test_routine_key_basic_slug() -> None:
    assert routine_key_from_label("Evening retinol") == "evening_retinol"


def test_routine_key_spf_synonym() -> None:
    assert routine_key_from_label("Reapply SPF") == "reapply_sunscreen"

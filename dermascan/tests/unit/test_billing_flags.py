"""Unit tests for meter/hold billing feature flags."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_flags_module():
    """Load ``flags.py`` without importing the full billing package."""
    module_path = Path(__file__).resolve().parents[2] / "billing" / "flags.py"
    spec = importlib.util.spec_from_file_location(
        "dermascan_billing_flags_under_test",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load billing flags module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBillingFlags:
    """Validate billing flag parsing and startup guards."""

    def test_validate_allows_meter_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shadow metering may run without enforcement."""
        flags = _load_flags_module()
        monkeypatch.setenv("BILLING_METER_ENABLED", "true")
        monkeypatch.setenv("BILLING_ENFORCE", "false")
        flags.validate_billing_flags()

    def test_validate_allows_meter_and_enforce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enforcement requires metering so settle has usage lines."""
        flags = _load_flags_module()
        monkeypatch.setenv("BILLING_METER_ENABLED", "true")
        monkeypatch.setenv("BILLING_ENFORCE", "true")
        flags.validate_billing_flags()

    def test_validate_rejects_enforce_without_meter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enforcement without metering would settle zero usage every turn."""
        flags = _load_flags_module()
        monkeypatch.setenv("BILLING_METER_ENABLED", "false")
        monkeypatch.setenv("BILLING_ENFORCE", "true")
        with pytest.raises(RuntimeError, match="BILLING_METER_ENABLED"):
            flags.validate_billing_flags()

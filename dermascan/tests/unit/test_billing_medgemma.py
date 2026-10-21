"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for MedGemma flat-SKU billing helpers."""


from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_billing_config_module():
    """Load ``config.py`` without importing the full billing package."""
    module_path = Path(__file__).resolve().parents[2] / "billing" / "config.py"
    spec = importlib.util.spec_from_file_location(
        "dermascan_billing_config_under_test",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load billing config module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMedgemmaJobTypeResolution:
    """Resolve billable job types from prompt hints."""

    def test_defaults_to_skin_audit(self) -> None:
        """Standard dermatology context maps to skin audit."""
        config = _load_billing_config_module()
        assert (
            config.resolve_medgemma_job_type("Visible redness on the cheeks.")
            == "skin_audit"
        )

    def test_detects_product_review(self) -> None:
        """Product label context maps to product review SKU."""
        config = _load_billing_config_module()
        assert (
            config.resolve_medgemma_job_type("Read the product label ingredient list.")
            == "product_review_image"
        )

    def test_detects_hybrid_review(self) -> None:
        """Hybrid prompts map to combined audit SKU."""
        config = _load_billing_config_module()
        assert (
            config.resolve_medgemma_job_type("Hybrid review of skin and product.")
            == "combined_audit"
        )

    def test_explicit_override(self) -> None:
        """Explicit job type wins over prompt heuristics."""
        config = _load_billing_config_module()
        assert (
            config.resolve_medgemma_job_type(
                "anything",
                explicit="product_review_image",
            )
            == "product_review_image"
        )

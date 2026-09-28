"""Unit tests for async product rules defer / interim messaging."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from dermascan.backend.services.agents import rules_index_jobs
from dermascan.backend.services.agents.rules_index_jobs import build_product_index_pending_message
from dermascan.rules_engine.engine.engine import IngredientIndexResult


@pytest.fixture(autouse=True)
def _redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.mark.unit
class TestIndexResultsRequireAsyncWait:
    """Defer detection from ensure_reports_exists outcomes."""

    def test_queued_requires_wait(self) -> None:
        """Any queued ingredient triggers defer."""
        results = [
            IngredientIndexResult(
                ingredient="A",
                normalized_name="a",
                indexed=False,
                source="queued",
            )
        ]
        assert rules_index_jobs.index_results_require_async_wait(results) is True

    def test_cached_does_not_require_wait(self) -> None:
        """Fully cached paths proceed immediately."""
        results = [
            IngredientIndexResult(
                ingredient="A",
                normalized_name="a",
                indexed=True,
                source="redis_cache",
            )
        ]
        assert rules_index_jobs.index_results_require_async_wait(results) is False


@pytest.mark.unit
class TestProductIndexPendingMessage:
    """Interim copy for users."""

    def test_build_message_includes_product_name(self) -> None:
        """Product name is substituted into the template."""
        text = build_product_index_pending_message("Test Serum")
        assert "Test Serum" in text
        assert "safety" in text.lower()


@pytest.mark.unit
class TestSaveAndClaimContext:
    """Redis helpers for defer idempotency."""

    def test_claim_pending_sent_once(self) -> None:
        """Second claim returns False."""
        redis_client = MagicMock()
        redis_client.set.side_effect = [True, False]
        assert rules_index_jobs.claim_product_index_pending_sent(redis_client, "req-1") is True
        assert rules_index_jobs.claim_product_index_pending_sent(redis_client, "req-1") is False

    def test_claim_final_report_sent_once(self) -> None:
        """Final report idempotency matches interim pattern."""
        redis_client = MagicMock()
        redis_client.set.side_effect = [True, False]
        assert rules_index_jobs.claim_product_final_report_sent(redis_client, "req-2") is True
        assert rules_index_jobs.claim_product_final_report_sent(redis_client, "req-2") is False


@pytest.mark.unit
class TestBatchTimeoutAndReconcile:
    """Batch watchdog helpers."""

    def test_is_index_batch_timed_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stale defer context exceeds configured timeout."""
        monkeypatch.setattr(rules_index_jobs, "RULES_INDEX_BATCH_TIMEOUT_SEC", 60)
        stale_ms = int(time.time() * 1000) - 120_000
        assert rules_index_jobs.is_index_batch_timed_out({"enqueued_at_ms": stale_ms}) is True
        fresh_ms = int(time.time() * 1000) - 1000
        assert rules_index_jobs.is_index_batch_timed_out({"enqueued_at_ms": fresh_ms}) is False

    def test_reconcile_orphan_index_batch(self) -> None:
        """Orphan SET members are cleared and counted."""
        redis_client = MagicMock()
        redis_client.smembers.return_value = {"niacinamide", "retinol"}
        cleared = rules_index_jobs.reconcile_orphan_index_batch(redis_client, "req-x")
        assert cleared == 2
        redis_client.delete.assert_called_once()

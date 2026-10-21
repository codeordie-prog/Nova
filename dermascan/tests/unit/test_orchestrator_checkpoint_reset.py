"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for orchestrator checkpoint reset helper."""


from __future__ import annotations

from unittest.mock import MagicMock, patch

from dermascan.shared.orchestrator_checkpoint_reset import reset_orchestrator_thread_checkpoint


def test_reset_skips_without_redis_url(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert reset_orchestrator_thread_checkpoint("254712345678") is False


def test_reset_deletes_thread_and_marker(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    mock_ctx = MagicMock()
    mock_checkpointer = MagicMock()
    mock_redis = MagicMock()

    with patch(
        "dermascan.shared.orchestrator_checkpoint_reset.open_checkpointer_url",
        return_value=(mock_checkpointer, mock_ctx),
    ):
        with patch(
            "dermascan.shared.orchestrator_checkpoint_reset.delete_thread",
            return_value=True,
        ) as mock_delete:
            with patch(
                "dermascan.shared.orchestrator_checkpoint_reset.get_redis_client",
                return_value=mock_redis,
            ):
                ok = reset_orchestrator_thread_checkpoint("254712345678")

    assert ok is True
    mock_delete.assert_called_once_with(mock_checkpointer, "254712345678")
    mock_redis.delete.assert_called_once_with("orchestrator:checkpoint_reset:254712345678")
    mock_ctx.__exit__.assert_called_once()

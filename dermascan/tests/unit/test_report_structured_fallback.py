"""Unit tests for report structured-output fallback routing."""

from __future__ import annotations

from dermascan.backend.services.agents.model_fallback_chain import (
    IncompleteStructuredOutputError,
    is_structured_output_error,
    should_fallback_to_next_model,
)
from dermascan.shared.concise_report_format import (
    CompactReportMessage,
    finalize_compact_report_body,
    normalize_compact_report_message,
)


def test_parse_errors_fall_through_model_chain() -> None:
    exc = ValueError(
        "Native structured output expected valid JSON for CompactReportMessage, "
        "but parsing failed: Unterminated string starting at: line 1 column 16."
    )
    assert is_structured_output_error(exc)
    assert should_fallback_to_next_model(exc)


def test_incomplete_structured_output_falls_through_model_chain() -> None:
    assert should_fallback_to_next_model(
        IncompleteStructuredOutputError("compact report message missing or too short")
    )


def test_normalize_compact_report_message_accepts_dict() -> None:
    text = normalize_compact_report_message(
        {
            "message": "La Roche-Posay SPF50\n• Use daily.\n• Reapply after sweating.",
        }
    )
    assert text is not None
    assert "La Roche-Posay" in text


def test_finalize_compact_report_body_appends_disclaimer() -> None:
    body = finalize_compact_report_body("Product review\n• Tip one.\n• Tip two.")
    assert "Educational only" in body


def test_compact_report_message_rejects_too_short() -> None:
    assert normalize_compact_report_message({"message": "short"}) is None
    assert normalize_compact_report_message(
        CompactReportMessage(
            message="Long enough product summary\n• First tip here.\n• Second tip here."
        )
    )

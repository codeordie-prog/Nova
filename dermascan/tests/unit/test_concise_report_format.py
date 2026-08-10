"""Unit tests for mobile micro-report assembly."""

from __future__ import annotations

from dermascan.shared.concise_report_format import (
    MicroReportFields,
    assemble_micro_report,
    render_micro_report,
)


def test_assemble_micro_report_preserves_separate_bullet_lines() -> None:
    text = assemble_micro_report(
        headline="Oatveen Eczema Moisturizing Cream: gentle barrier support",
        bullets=[
            "Apply to damp skin to lock in moisture.",
            "Patch-test first if your skin is easily irritated.",
        ],
        disclaimer="Educational only — not a medical diagnosis.",
    )
    lines = text.splitlines()
    assert lines[0].startswith("Oatveen")
    assert lines[2].startswith("• Apply to damp skin")
    assert lines[3].startswith("• Patch-test first")


def test_render_micro_report_from_structured_fields() -> None:
    rendered = render_micro_report(
        MicroReportFields(
            headline="Visible redness may suggest mild irritation",
            bullets=["Use a gentle cleanser.", "Avoid harsh scrubs for now."],
            seek_care="We can connect you with a dermatologist — reply yes if you'd like that.",
            disclaimer="Educational only — not a medical diagnosis.",
        )
    )
    assert rendered.count("• ") == 2
    assert "connect you with a dermatologist" in rendered


def test_strips_bullet_prefixes_from_model_bullets() -> None:
    rendered = render_micro_report(
        MicroReportFields(
            headline="Headline",
            bullets=["• First tip", "- Second tip"],
        )
    )
    assert "• First tip" in rendered
    assert "• Second tip" in rendered
    assert "- Second" not in rendered

"""Unit tests for mobile micro-report assembly."""

from __future__ import annotations

from dermascan.shared.concise_report_format import (
    COMPACT_REPORT_BODY_MAX_CHARS,
    REPORT_BULLET_MAX_CHARS,
    REPORT_FOOTER_MAX_CHARS,
    REPORT_HEADLINE_MAX_CHARS,
    REPORT_MAX_BULLETS,
    REPORT_MAX_CHARS,
    CompactReportMessage,
    MicroReportFields,
    assemble_micro_report,
    compact_message_format_instructions,
    render_micro_report,
)


def test_compact_message_format_instructions_mentions_single_field() -> None:
    instructions = compact_message_format_instructions()
    assert "`message`" in instructions
    assert str(COMPACT_REPORT_BODY_MAX_CHARS) in instructions


def test_compact_report_message_enforces_max_length() -> None:
    long_body = "Title line\n" + ("• Tip.\n" * 2) + ("x" * COMPACT_REPORT_BODY_MAX_CHARS)
    fields = CompactReportMessage.model_validate({"message": long_body})
    assert len(fields.message) <= COMPACT_REPORT_BODY_MAX_CHARS


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


def test_micro_report_fields_truncate_overlong_copy() -> None:
    fields = MicroReportFields(
        headline="H" * 400,
        bullets=["B" * 400, "C" * 400, "D" * 400],
        seek_care="S" * 400,
        disclaimer="D" * 400,
    )
    assert len(fields.headline) <= REPORT_HEADLINE_MAX_CHARS
    assert len(fields.bullets) == REPORT_MAX_BULLETS
    assert all(len(item) <= REPORT_BULLET_MAX_CHARS for item in fields.bullets)
    assert len(fields.seek_care) <= REPORT_FOOTER_MAX_CHARS
    assert len(fields.disclaimer) <= REPORT_FOOTER_MAX_CHARS
    rendered = render_micro_report(fields)
    assert len(rendered) <= REPORT_MAX_CHARS


def test_coerce_string_bullets_into_list() -> None:
    fields = MicroReportFields.model_validate(
        {"headline": "Headline", "bullets": "First tip\nSecond tip"}
    )
    assert len(fields.bullets) == 2


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

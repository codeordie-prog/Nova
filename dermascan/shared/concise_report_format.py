"""
Author: Dermascan Team
Date: 2026-08-10
Version: 1.0.0
Description: Mobile micro-report helpers: prompt budgets and assembly (no text rewriting).
Mobile micro-report helpers: prompt budgets and assembly (no text rewriting)."""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    pass

log: logging.Logger = logging.getLogger(__name__)

REPORT_MAX_CHARS: int = max(280, int(os.getenv("SIGNAL_REPORT_MAX_CHARS", "600")))
REPORT_HEADLINE_MAX_CHARS: int = max(60, int(os.getenv("SIGNAL_REPORT_HEADLINE_MAX_CHARS", "120")))
REPORT_BULLET_MAX_CHARS: int = max(40, int(os.getenv("SIGNAL_REPORT_BULLET_MAX_CHARS", "100")))
REPORT_MAX_BULLETS: int = max(1, int(os.getenv("SIGNAL_REPORT_MAX_BULLETS", "2")))
REPORT_FOOTER_MAX_CHARS: int = max(40, int(os.getenv("SIGNAL_REPORT_FOOTER_MAX_CHARS", "120")))

DEFAULT_DISCLAIMER: str = "Educational only — not a medical diagnosis or prescription."

DERMATOLOGIST_CONNECTION_OFFER: str = (
    "We can connect you with a dermatologist for a professional review — reply yes if you'd like that."
)


def dermatologist_connection_guidance() -> str:
    """
    Return prompt guidance for offering in-chat dermatologist connection.

    Returns:
        str: Instruction fragment for orchestrator and report subagents.
    """
    return (
        "When dermatology input may help, do NOT tell the user to find, visit, or 'see a dermatologist' "
        "on their own. Offer that we can connect them with a dermatologist through this chat "
        f'(e.g. "{DERMATOLOGIST_CONNECTION_OFFER}"). '
        "Use 'seek emergency/immediate care' wording only for true medical emergencies."
    )


def micro_report_format_instructions(*, max_chars: int = REPORT_MAX_CHARS) -> str:
    """
    Return shared LLM instructions for structured mobile micro-reports.

    Args:
        max_chars: Total character budget for the assembled report body.

    Returns:
        str: Prompt fragment describing required structured fields.
    """
    return (
        "Return structured micro-report fields for WhatsApp/Telegram (not one merged paragraph):\n"
        f"- headline: one short line (max {REPORT_HEADLINE_MAX_CHARS} chars).\n"
        f"- bullets: exactly {REPORT_MAX_BULLETS} separate tips as list items WITHOUT bullet prefixes "
        f"(max {REPORT_BULLET_MAX_CHARS} chars each). Never combine two tips into one string.\n"
        f"- seek_care: one optional line when a dermatologist may help, else empty string "
        f"(max {REPORT_FOOTER_MAX_CHARS} chars). Offer in-chat connection — "
        f'"{DERMATOLOGIST_CONNECTION_OFFER}" — not an external referral.\n'
        f"- disclaimer: one short educational line (max {REPORT_FOOTER_MAX_CHARS} chars).\n"
        f"Shorten wording to fit budgets. Total assembled body under {max_chars} chars. "
        "Plain text only. No markdown. No URLs."
    )


def strip_report_markdown(text: str) -> str:
    """
    Strip lightweight markdown from a report fragment.

    Args:
        text: Raw fragment from the model.

    Returns:
        str: Plain-text fragment.
    """
    cleaned = str(text or "")
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _strip_bullet_prefix(text: str) -> str:
    stripped = str(text or "").strip()
    return re.sub(r"^[\•\-\*]\s+", "", stripped).strip()


class MicroReportFields(BaseModel):
    """Structured user-facing audit report fields produced by report subagents."""

    headline: str = ""
    bullets: list[str] = Field(default_factory=list)
    seek_care: str = ""
    disclaimer: str = DEFAULT_DISCLAIMER

    @field_validator("bullets")
    @classmethod
    def _limit_bullets(cls, value: list[str]) -> list[str]:
        cleaned = [_strip_bullet_prefix(str(item)) for item in value if str(item).strip()]
        return [item for item in cleaned if item][:REPORT_MAX_BULLETS]

    @field_validator("headline", "seek_care", "disclaimer")
    @classmethod
    def _strip_field(cls, value: str) -> str:
        return strip_report_markdown(value)


def assemble_micro_report(
    *,
    headline: str,
    bullets: list[str],
    seek_care: str = "",
    disclaimer: str = DEFAULT_DISCLAIMER,
) -> str:
    """
    Join structured micro-report fields into outbound plain text.

    Does not truncate or rewrite content — the model is responsible for length.

    Args:
        headline: Single summary line.
        bullets: Tip strings without bullet prefixes.
        seek_care: Optional seek-care line.
        disclaimer: Educational disclaimer line.

    Returns:
        str: Assembled report text.
    """
    parts: list[str] = []
    headline_text = strip_report_markdown(headline)
    if headline_text:
        parts.append(headline_text)

    bullet_lines: list[str] = []
    for bullet in bullets[:REPORT_MAX_BULLETS]:
        body = _strip_bullet_prefix(strip_report_markdown(bullet))
        if body:
            bullet_lines.append(f"• {body}")

    if bullet_lines:
        if parts:
            parts.append("")
        parts.extend(bullet_lines)

    for footer in (seek_care, disclaimer):
        footer_text = strip_report_markdown(footer)
        if footer_text:
            parts.append(footer_text)

    return "\n".join(parts).strip()


def render_micro_report(fields: MicroReportFields) -> str:
    """
    Render structured micro-report fields for outbound delivery.

    Args:
        fields: Structured report content from a report subagent.

    Returns:
        str: Assembled plain-text report.
    """
    rendered = assemble_micro_report(
        headline=fields.headline,
        bullets=fields.bullets,
        seek_care=fields.seek_care,
        disclaimer=fields.disclaimer,
    )
    if rendered and len(rendered) > REPORT_MAX_CHARS:
        log.warning(
            "micro-report exceeds budget: chars=%s budget=%s headline_len=%s bullet_count=%s",
            len(rendered),
            REPORT_MAX_CHARS,
            len(fields.headline),
            len(fields.bullets),
        )
    return rendered


def build_static_micro_report(
    *,
    headline: str,
    bullets: list[str],
    seek_care: str = "",
    disclaimer: str = DEFAULT_DISCLAIMER,
) -> str:
    """
    Build a deterministic fallback micro-report from static copy.

    Args:
        headline: Summary headline.
        bullets: Bullet bodies without prefixes.
        seek_care: Optional seek-care line.
        disclaimer: Disclaimer line.

    Returns:
        str: Assembled report text.
    """
    return render_micro_report(
        MicroReportFields(
            headline=headline,
            bullets=bullets,
            seek_care=seek_care,
            disclaimer=disclaimer,
        )
    )

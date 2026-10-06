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

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

if TYPE_CHECKING:
    pass

log: logging.Logger = logging.getLogger(__name__)

REPORT_MAX_CHARS: int = max(280, int(os.getenv("SIGNAL_REPORT_MAX_CHARS", "600")))
REPORT_HEADLINE_MAX_CHARS: int = max(60, int(os.getenv("SIGNAL_REPORT_HEADLINE_MAX_CHARS", "120")))
REPORT_BULLET_MAX_CHARS: int = max(40, int(os.getenv("SIGNAL_REPORT_BULLET_MAX_CHARS", "100")))
REPORT_MAX_BULLETS: int = max(1, int(os.getenv("SIGNAL_REPORT_MAX_BULLETS", "2")))
REPORT_FOOTER_MAX_CHARS: int = max(40, int(os.getenv("SIGNAL_REPORT_FOOTER_MAX_CHARS", "120")))
COMPACT_REPORT_BODY_MAX_CHARS: int = min(
    REPORT_MAX_CHARS,
    max(200, int(os.getenv("SIGNAL_REPORT_COMPACT_BODY_MAX_CHARS", "520"))),
)
COMPACT_REPORT_MIN_BODY_CHARS: int = max(
    20, int(os.getenv("SIGNAL_REPORT_COMPACT_MIN_BODY_CHARS", "40"))
)

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


def compact_message_format_instructions(
    *,
    max_chars: int = COMPACT_REPORT_BODY_MAX_CHARS,
) -> str:
    """
    Return LLM instructions for a single compact plain-text report body.

    Args:
        max_chars: Character budget for the model-produced ``message`` field only.

    Returns:
        str: Prompt fragment for one-field structured output.
    """
    return (
        "Return one plain-text `message` for WhatsApp/Telegram (not JSON with separate fields):\n"
        "- Line 1: short title or summary.\n"
        f"- Lines 2–3: exactly {REPORT_MAX_BULLETS} short tips (you may prefix with •).\n"
        "- When dermatology input may help, weave a brief in-chat connection offer into a tip "
        f'(e.g. "{DERMATOLOGIST_CONNECTION_OFFER}") — not an external referral.\n'
        f"Keep the entire `message` under {max_chars} characters. "
        "Do NOT include disclaimer, legal/educational footer, or URLs (those are appended in code). "
        "Plain text only. No markdown."
    )


class CompactReportMessage(BaseModel):
    """Single-field compact report body from the formatter LLM."""

    message: str = Field(
        ...,
        description=(
            f"Compact user-facing report: title line plus {REPORT_MAX_BULLETS} tips. "
            f"Max {COMPACT_REPORT_BODY_MAX_CHARS} characters. No disclaimer or URLs."
        ),
        min_length=COMPACT_REPORT_MIN_BODY_CHARS,
        max_length=COMPACT_REPORT_BODY_MAX_CHARS,
    )

    @field_validator("message", mode="before")
    @classmethod
    def _strip_message(cls, value: object) -> str:
        return strip_report_markdown(str(value or ""))[:COMPACT_REPORT_BODY_MAX_CHARS]


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

    headline: str = Field(
        default="",
        description=(
            f"One short summary line. Max {REPORT_HEADLINE_MAX_CHARS} characters. "
            "Plain text only. No markdown."
        ),
        max_length=REPORT_HEADLINE_MAX_CHARS,
    )
    bullets: list[str] = Field(
        default_factory=list,
        description=(
            f"Exactly {REPORT_MAX_BULLETS} short tips as separate list items, "
            f"without bullet prefixes. Each tip max {REPORT_BULLET_MAX_CHARS} characters."
        ),
        min_length=1,
        max_length=REPORT_MAX_BULLETS,
    )
    seek_care: str = Field(
        default="",
        description=(
            f"Optional in-chat dermatologist offer, else empty. "
            f"Max {REPORT_FOOTER_MAX_CHARS} characters."
        ),
        max_length=REPORT_FOOTER_MAX_CHARS,
    )
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description=(
            f"One short educational disclaimer. Max {REPORT_FOOTER_MAX_CHARS} characters."
        ),
        max_length=REPORT_FOOTER_MAX_CHARS,
    )

    @field_validator("bullets", mode="before")
    @classmethod
    def _limit_bullets(cls, value: object) -> list[str]:
        raw_items: list[object]
        if isinstance(value, list):
            raw_items = list(value)
        elif isinstance(value, tuple):
            raw_items = list(value)
        elif isinstance(value, str) and value.strip():
            raw_items = [line for line in value.splitlines() if line.strip()]
            if len(raw_items) <= 1 and value.strip():
                raw_items = [value.strip()]
        else:
            raw_items = []
        cleaned = [_strip_bullet_prefix(str(item)) for item in raw_items if str(item).strip()]
        trimmed = [item[:REPORT_BULLET_MAX_CHARS] for item in cleaned if item]
        return trimmed[:REPORT_MAX_BULLETS]

    @field_validator("headline", "seek_care", "disclaimer", mode="before")
    @classmethod
    def _strip_field(cls, value: object, info: ValidationInfo) -> str:
        cleaned = strip_report_markdown(str(value or ""))
        limits: dict[str, int] = {
            "headline": REPORT_HEADLINE_MAX_CHARS,
            "seek_care": REPORT_FOOTER_MAX_CHARS,
            "disclaimer": REPORT_FOOTER_MAX_CHARS,
        }
        return cleaned[: limits.get(str(info.field_name), REPORT_HEADLINE_MAX_CHARS)]


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


def coerce_micro_report_fields(
    raw: object,
    *,
    schema: type[MicroReportFields] = MicroReportFields,
) -> MicroReportFields | None:
    """
    Normalize structured model output into micro-report fields.

    Args:
        raw: Parsed model output (Pydantic model or dict).
        schema: Target schema class (e.g. product or skin report).

    Returns:
        MicroReportFields | None: Coerced fields, or None when parsing fails.
    """
    if isinstance(raw, schema):
        return raw
    if isinstance(raw, MicroReportFields):
        try:
            return schema.model_validate(raw.model_dump(mode="json"))
        except ValidationError:
            return None
    payload: dict[str, object] | None = None
    if isinstance(raw, BaseModel):
        payload = raw.model_dump(mode="json")
    elif isinstance(raw, dict):
        payload = dict(raw)
    if payload is None:
        return None
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        log.warning("micro-report coerce failed: %s", exc)
        return None


def normalize_compact_report_message(raw: object) -> str | None:
    """
    Extract and validate a compact report body from structured model output.

    Args:
        raw: Parsed model output (Pydantic model or dict).

    Returns:
        str | None: Normalized message text, or None when missing or too short.
    """
    text: str = ""
    if isinstance(raw, CompactReportMessage):
        text = raw.message
    elif isinstance(raw, BaseModel):
        candidate = getattr(raw, "message", None)
        text = str(candidate) if candidate is not None else ""
    elif isinstance(raw, dict):
        text = str(raw.get("message") or "")
    else:
        return None
    cleaned = strip_report_markdown(text).strip()
    if len(cleaned) < COMPACT_REPORT_MIN_BODY_CHARS:
        return None
    return cleaned[:COMPACT_REPORT_BODY_MAX_CHARS]


def finalize_compact_report_body(
    message: str,
    *,
    append_disclaimer: bool = True,
) -> str:
    """
    Prepare compact LLM body for outbound delivery.

    Args:
        message: Model-produced report body (title + tips).
        append_disclaimer: When True, append the standard educational disclaimer.

    Returns:
        str: Body ready for mode/guidance footers and source attachment.
    """
    body = strip_report_markdown(message).strip()
    if not body:
        return ""
    if append_disclaimer and DEFAULT_DISCLAIMER.lower() not in body.lower():
        body = f"{body}\n{DEFAULT_DISCLAIMER}"
    return body.strip()


def usable_micro_report(fields: object) -> bool:
    """
    Return True when structured fields can be assembled for outbound delivery.

    Args:
        fields: Parsed model output.

    Returns:
        bool: True when a headline and at least one bullet are present.
    """
    coerced = (
        fields
        if isinstance(fields, MicroReportFields)
        else coerce_micro_report_fields(fields)
    )
    return (
        coerced is not None
        and bool(coerced.headline.strip())
        and bool(coerced.bullets)
    )


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

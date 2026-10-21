"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-10-17
License: MIT

Product and skin report prompt helpers: template structure, brand priority, name resolution.
"""


from __future__ import annotations

import os
import re
from typing import Any

from dermascan.shared.concise_report_format import (
    COMPACT_REPORT_BODY_MAX_CHARS,
    DERMATOLOGIST_CONNECTION_OFFER,
    compact_message_format_instructions,
    dermatologist_connection_guidance,
    strip_report_markdown,
)


def load_priority_recommendation_brands() -> list[str]:
    """
    Load ordered priority brand names for product recommendations (internal use only).

    Returns:
        list[str]: Brand names, highest priority first. Empty when unset.
    """
    raw = str(os.getenv("PRODUCT_RECOMMENDATION_PRIORITY_BRANDS", "") or "").strip()
    if not raw:
        return []
    brands: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in brands:
            brands.append(name)
    return brands


def priority_brands_prompt_clause() -> str:
    """
    Return LLM instructions to prefer configured brands when suggesting products.

    Returns:
        str: Prompt fragment, or empty when no brands configured.
    """
    brands = load_priority_recommendation_brands()
    if not brands:
        return (
            "When naming alternative or routine products, use reputable, widely available brands. "
            "Do not mention sponsorship or paid partnerships."
        )
    joined = ", ".join(brands)
    return (
        "When naming alternative or routine products, prefer suitable options from these brands "
        f"(highest priority first): {joined}. "
        "If none fit the user's profile, name other reputable products. "
        "Do not mention sponsorship, partnerships, or that a list was used."
    )


def ensure_dermatologist_connection_offer(text: str) -> str:
    """
    Append the standard in-chat dermatologist offer when the report omitted it.

    Args:
        text: Report body.

    Returns:
        str: Text with offer present.
    """
    body = strip_report_markdown(text).strip()
    if not body:
        return DERMATOLOGIST_CONNECTION_OFFER
    lowered = body.lower()
    if "dermatologist" in lowered and "reply yes" in lowered:
        return body
    return f"{body}\n{DERMATOLOGIST_CONNECTION_OFFER}"


def _clean_resolved_product_name(candidate: str) -> str:
    """
    Trim conversational trailing phrases from a resolved product name.

    Args:
        candidate: Raw captured name text.

    Returns:
        str: Cleaned product name.
    """
    cleaned = candidate.strip().strip('"').strip("'")
    lowered = cleaned.lower()
    for suffix in (
        " for your check",
        " for your review",
        " for your product check",
        " for you",
    ):
        if lowered.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            lowered = cleaned.lower()
    return cleaned[:120]


def _entry_text(entry: dict[str, Any]) -> str:
    for key in ("content", "text", "message", "body"):
        value = entry.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def resolve_product_display_name(
    product_name: str,
    *,
    context: list[dict[str, Any]] | None = None,
    description: str = "",
) -> str:
    """
    Resolve a product display name from payload fields or recent conversation.

    Args:
        product_name: Name from the signal payload.
        context: Recent orchestrator conversation entries.
        description: Optional description fallback.

    Returns:
        str: Best available product name, or empty string.
    """
    cleaned = str(product_name or "").strip()
    if cleaned:
        return cleaned
    desc = str(description or "").strip()
    if desc and len(desc) <= 120 and "\n" not in desc:
        return desc

    if not context:
        return ""

    name_patterns = (
        re.compile(r"(?i)(?:safety review for|reviewing|checking|product check(?: for)?)[:\s]+(.+?)(?:[\.!\?]|$)"),
        re.compile(r"(?i)(?:label for|product)[:\s]+(.+?)(?:[\.!\?]|$)"),
    )
    for entry in reversed(context[-15:]):
        text = _entry_text(entry)
        if not text:
            continue
        for pattern in name_patterns:
            match = pattern.search(text)
            if match:
                candidate = _clean_resolved_product_name(match.group(1))
                if len(candidate) >= 4:
                    return candidate

    for entry in reversed(context[-8:]):
        role = str(entry.get("role") or entry.get("type") or "").lower()
        if role and role not in {"user", "human"}:
            continue
        text = _entry_text(entry)
        if 4 <= len(text) <= 120:
            return text[:120]
    return ""


def product_safety_report_format_instructions(
    *,
    max_chars: int = COMPACT_REPORT_BODY_MAX_CHARS,
) -> str:
    """
    Return LLM instructions for structured product safety reports.

    Args:
        max_chars: Character budget for the compact ``message`` field.

    Returns:
        str: Prompt fragment describing required layout and content rules.
    """
    return (
        f"{compact_message_format_instructions(max_chars=max_chars)}\n"
        f"{dermatologist_connection_guidance()}\n"
        f"{priority_brands_prompt_clause()}\n"
        "Required plain-text layout inside `message` (compressed, no filler):\n"
        "Line 1: {Product name}: Safety Review\n"
        "Line 2: Function: <one short phrase — what the product is for>\n"
        "Then up to 3 bullet lines starting with • (safety themes for THIS user when profile exists).\n"
        "When ANY caution or contraindication applies for the user's condition or profile, you MUST add "
        "1-2 named alternative products (a different product type is OK if it better serves their goal). "
        "For each alternative include: Better for you because: <short reason>.\n"
        "When profile/skin context shows the reviewed product is a poor fit but ingredients were partial, "
        "still give best-effort guidance plus alternatives.\n"
        f"ALWAYS include the in-chat dermatologist connection offer "
        f'(e.g. "{DERMATOLOGIST_CONNECTION_OFFER}").\n'
        "Do not include disclaimer or source URLs in `message`."
    )


def skin_review_with_routine_format_instructions(
    *,
    max_chars: int = COMPACT_REPORT_BODY_MAX_CHARS,
) -> str:
    """
    Return LLM instructions for skin audit reports with a light product routine.

    Args:
        max_chars: Character budget for the compact ``message`` field.

    Returns:
        str: Prompt fragment for skin-only completion signals.
    """
    return (
        f"{compact_message_format_instructions(max_chars=max_chars)}\n"
        f"{dermatologist_connection_guidance()}\n"
        f"{priority_brands_prompt_clause()}\n"
        "Required layout inside `message`:\n"
        "Line 1: short headline on visible skin pattern (cautious wording).\n"
        "Line 2: Function: <what the user should focus on next — e.g. barrier support, sun protection>.\n"
        "Then up to 3 bullet lines starting with • (findings + practical tips).\n"
        "Include a compact routine recommendation: 1-3 product types with specific product names "
        "(cleanser, moisturizer, treatment, SPF as relevant — not a long regimen).\n"
        f"ALWAYS include the in-chat dermatologist connection offer "
        f'(e.g. "{DERMATOLOGIST_CONNECTION_OFFER}").\n'
        "Do not include disclaimer or URLs in `message`."
    )

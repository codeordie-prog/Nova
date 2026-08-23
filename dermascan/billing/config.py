"""
Author: Dermascan Team
Date: 2026-08-21
License: MIT

Billing configuration and enums locked in Phase 0.

See cloud_sql/billing/PHASE0_DECISIONS.md and cloud_sql/billing/RATE_CARD.md.
Module path: ``dermascan.billing.config``.

Rate card v2026-08-21:
  Internal credits use **100 credits = $1.00 USD** of provider API cost
  (``BILLING_CREDITS_PER_USD``). Retail KES ↔ credits is set at Paystack top-up.

Sources (paid tier, standard context, per 1M tokens):
  - OpenAI: https://developers.openai.com/api/docs/pricing (Aug 2026)
  - Gemini: https://ai.google.dev/gemini-api/docs/pricing (Aug 2026)



Meter types:
  - LLM: LLM call - Orchestrator API call, Summarization chain fallback
  - Tool: Tool call - Orchestrator API call, Summarization chain fallback
  - Inference: MedGemma inference - Medgemma API call

Orchestrator API call and summarization chai fallbacks are metering token usage.
Inference is metering flat job type cost + token usage.

"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Final, Optional


class LedgerKind(StrEnum):
    """Append-only ledger entry kinds."""

    GRANT = "grant"
    TOPUP = "topup"
    HOLD = "hold"
    RELEASE = "release"
    DEBIT = "debit"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


class ReservationState(StrEnum):
    """Reservation lifecycle states."""

    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"


class MeterType(StrEnum):
    """Usage line meter types."""

    LLM = "llm"
    TOOL = "tool"
    INFERENCE = "inference"


class LlmPurpose(StrEnum):
    """LLM call purpose within a billable scope."""

    AGENT_STEP = "agent_step"
    SUMMARIZATION = "summarization"


class BillingJobType(StrEnum):
    """Billable job types for reservations."""

    ORCHESTRATOR_TURN = "orchestrator_turn"
    SKIN_AUDIT = "skin_audit"
    PRODUCT_REVIEW_IMAGE = "product_review_image"
    COMBINED_AUDIT = "combined_audit"


_MEDGEMMA_PRODUCT_REVIEW_HINTS: Final[tuple[str, ...]] = (
    "product review",
    "product_review",
    "ingredient list",
    "product label",
)
_MEDGEMMA_HYBRID_HINTS: Final[tuple[str, ...]] = (
    "hybrid review",
    "hybrid_review",
    "skin and product",
)


def resolve_medgemma_job_type(
    prompt: str,
    *,
    explicit: Optional[str] = None,
) -> str:
    """
    Resolve the billable MedGemma job type from explicit input or prompt text.

    Args:
        prompt: Inference prompt or audit context.
        explicit: Optional override (:class:`BillingJobType` value).

    Returns:
        str: Job type label stored on reservations and usage lines.
    """
    if explicit:
        normalized = explicit.strip().lower()
        for job_type in BillingJobType:
            if normalized == job_type.value:
                return job_type.value
        return normalized

    lowered = (prompt or "").lower()
    if any(hint in lowered for hint in _MEDGEMMA_HYBRID_HINTS):
        return BillingJobType.COMBINED_AUDIT.value
    if any(hint in lowered for hint in _MEDGEMMA_PRODUCT_REVIEW_HINTS):
        return BillingJobType.PRODUCT_REVIEW_IMAGE.value
    return BillingJobType.SKIN_AUDIT.value


class BillingRefType(StrEnum):
    """Ledger reference types."""

    RESERVATION = "reservation"
    PAYMENT = "payment"
    MANUAL = "manual"
    USAGE_SETTLEMENT = "usage_settlement"


# --- Credit scale --------------------------------------------------------------

# 100 internal credits = $1.00 of provider API spend (before retail markup).
BILLING_CREDITS_PER_USD: Final[int] = max(
    1, int(os.getenv("BILLING_CREDITS_PER_USD", "100"))
)


def _usd_per_1m_to_credits(usd_per_1m: float) -> int:
    """
    Convert a USD-per-1M-token list price to internal credits per 1M tokens.

    Args:
        usd_per_1m: Provider list price in USD per million tokens.

    Returns:
        int: Credits per 1M tokens (rounded up to nearest integer).
    """
    if usd_per_1m <= 0:
        return 0
    micro_usd: int = int(round(usd_per_1m * 1_000_000))
    credits: int = (micro_usd * BILLING_CREDITS_PER_USD + 999_999) // 1_000_000
    return max(1, credits)


def _rate_from_env(env_key: str, *, usd_fallback: float) -> int:
    """
    Read credits-per-1M from env or derive from USD fallback.

    Args:
        env_key: Environment variable name.
        usd_fallback: USD per 1M tokens when env is unset or zero.

    Returns:
        int: Credits per 1M tokens.
    """
    raw: str = os.getenv(env_key, "").strip()
    if raw:
        return max(0, int(raw))
    return _usd_per_1m_to_credits(usd_fallback)


# --- Hold ceilings (credits) ---------------------------------------------------

BILLING_ORCHESTRATOR_HOLD_CREDITS: Final[int] = max(
    1, int(os.getenv("BILLING_ORCHESTRATOR_HOLD_CREDITS", "150"))
)
BILLING_MEDGEMMA_HOLD_CREDITS: Final[int] = max(
    1, int(os.getenv("BILLING_MEDGEMMA_HOLD_CREDITS", "200"))
)
BILLING_SKIN_AUDIT_CREDITS: Final[int] = max(
    0, int(os.getenv("BILLING_SKIN_AUDIT_CREDITS", "40"))
)
BILLING_PRODUCT_REVIEW_IMAGE_CREDITS: Final[int] = max(
    0, int(os.getenv("BILLING_PRODUCT_REVIEW_IMAGE_CREDITS", "50"))
)
BILLING_COMBINED_AUDIT_CREDITS: Final[int] = max(
    0, int(os.getenv("BILLING_COMBINED_AUDIT_CREDITS", "60"))
)

BILLING_MEDGEMMA_JOB_CREDITS: Final[dict[str, int]] = {
    BillingJobType.SKIN_AUDIT.value: BILLING_SKIN_AUDIT_CREDITS,
    BillingJobType.PRODUCT_REVIEW_IMAGE.value: BILLING_PRODUCT_REVIEW_IMAGE_CREDITS,
    BillingJobType.COMBINED_AUDIT.value: BILLING_COMBINED_AUDIT_CREDITS,
}
BILLING_MIN_BALANCE_CREDITS: Final[int] = max(
    0, int(os.getenv("BILLING_MIN_BALANCE_CREDITS", "150"))
)
BILLING_FREE_GRANT_CREDITS: Final[int] = max(
    0, int(os.getenv("BILLING_FREE_GRANT_CREDITS", "0"))
)
BILLING_RESERVATION_TTL_SEC: Final[int] = max(
    60, int(os.getenv("BILLING_RESERVATION_TTL_SEC", "900"))
)

# --- Rate card (token → credits) -----------------------------------------------

BILLING_RATE_CARD_VERSION: Final[str] = os.getenv(
    "BILLING_RATE_CARD_VERSION", "2026-08-21"
).strip()

# Orchestrator primary chain (model_fallback_chain._DEFAULT_ORCHESTRATOR_MODEL_CHAIN)
# gemini-3.5-flash:        $1.50 in / $9.00 out
# gemini-3.1-flash-lite:   $0.25 in / $1.50 out
# gpt-4o:                  $2.50 in / $10.00 out
# gpt-5:                   $1.25 in / $10.00 out

BILLING_TOKEN_RATES: Final[dict[str, dict[str, int]]] = {
    "google:gemini-3.5-flash": {
        "input_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_35_FLASH_INPUT_1M", usd_fallback=1.50
        ),
        "output_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_35_FLASH_OUTPUT_1M", usd_fallback=9.00
        ),
        "cached_input_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_35_FLASH_CACHED_INPUT_1M", usd_fallback=0.15
        ),
    },
    "google:gemini-3.1-flash-lite": {
        "input_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_31_FLASH_LITE_INPUT_1M", usd_fallback=0.25
        ),
        "output_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_31_FLASH_LITE_OUTPUT_1M", usd_fallback=1.50
        ),
        "cached_input_per_1m_credits": _rate_from_env(
            "BILLING_GEMINI_31_FLASH_LITE_CACHED_INPUT_1M", usd_fallback=0.025
        ),
    },
    "openai:gpt-4o": {
        "input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_INPUT_1M", usd_fallback=2.50
        ),
        "output_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_OUTPUT_1M", usd_fallback=10.00
        ),
        "cached_input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_CACHED_INPUT_1M", usd_fallback=1.25
        ),
    },
    "openai:gpt-5": {
        "input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT5_INPUT_1M", usd_fallback=1.25
        ),
        "output_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT5_OUTPUT_1M", usd_fallback=10.00
        ),
        "cached_input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT5_CACHED_INPUT_1M", usd_fallback=0.125
        ),
    },
    # Summarization chain fallback
    "openai:gpt-4o-mini": {
        "input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_MINI_INPUT_1M", usd_fallback=0.15
        ),
        "output_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_MINI_OUTPUT_1M", usd_fallback=0.60
        ),
        "cached_input_per_1m_credits": _rate_from_env(
            "BILLING_OPENAI_GPT4O_MINI_CACHED_INPUT_1M", usd_fallback=0.075
        ),
    },
}


def credits_for_tokens(
    *,
    provider_model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> int:
    """
    Convert token counts to integer credits using the active rate card.

    Args:
        provider_model: Key such as ``openai:gpt-4o`` or ``google:gemini-3.5-flash``.
        input_tokens: Non-cached prompt/input token count.
        output_tokens: Completion/output token count.
        cached_input_tokens: Cached prompt tokens (priced separately when configured).

    Returns:
        int: Non-negative credits (rounded up per token bucket).
    """
    rates = BILLING_TOKEN_RATES.get(provider_model)
    if rates is None:
        return 0

    def _bucket(count: int, per_1m: int) -> int:
        if count <= 0 or per_1m <= 0:
            return 0
        return (count * per_1m + 999_999) // 1_000_000

    in_credits = _bucket(max(0, input_tokens), int(rates.get("input_per_1m_credits", 0)))
    out_credits = _bucket(max(0, output_tokens), int(rates.get("output_per_1m_credits", 0)))
    cached_credits = _bucket(
        max(0, cached_input_tokens),
        int(rates.get("cached_input_per_1m_credits", 0)),
    )
    return in_credits + out_credits + cached_credits


def credits_for_medgemma_job(job_type: str) -> int:
    """
    Return flat credits for a completed MedGemma inference job.

    Args:
        job_type: :class:`BillingJobType` value.

    Returns:
        int: Non-negative credits for one successful job.
    """
    return max(0, int(BILLING_MEDGEMMA_JOB_CREDITS.get(job_type, 0)))


__all__ = [
    "BILLING_CREDITS_PER_USD",
    "BILLING_FREE_GRANT_CREDITS",
    "BILLING_COMBINED_AUDIT_CREDITS",
    "BILLING_MEDGEMMA_HOLD_CREDITS",
    "BILLING_MEDGEMMA_JOB_CREDITS",
    "BILLING_PRODUCT_REVIEW_IMAGE_CREDITS",
    "BILLING_SKIN_AUDIT_CREDITS",
    "BILLING_MIN_BALANCE_CREDITS",
    "BILLING_ORCHESTRATOR_HOLD_CREDITS",
    "BILLING_RATE_CARD_VERSION",
    "BILLING_RESERVATION_TTL_SEC",
    "BILLING_TOKEN_RATES",
    "BillingJobType",
    "BillingRefType",
    "LedgerKind",
    "LlmPurpose",
    "MeterType",
    "ReservationState",
    "credits_for_medgemma_job",
    "credits_for_tokens",
    "resolve_medgemma_job_type",
]

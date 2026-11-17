"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-21
Licence: PolyForm Noncommercial License 1.0.0

Billing configuration and enums locked in Phase 0.

See cloud_sql/billing/PHASE0_DECISIONS.md and cloud_sql/billing/RATE_CARD.md.
Module path: ``dermascan.billing.config``.

Credits are KES: one credit is one shilling. Provider token prices are quoted
in USD per 1M tokens and converted through the ``usd_kes_rate`` setting in
``billing.price_list``. Costs accumulate in micro-credits and round once, at
settle.

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
from decimal import Decimal
from enum import StrEnum
from typing import Final, Optional

from dermascan.shared.logger import get_logger


logger = get_logger(__name__)


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
    SPECIALIST_ESCALATION = "specialist_escalation"


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
#
# One credit is one Kenyan shilling. That is what a user tops up through
# Paystack, sees in chat, and spends.
#
# Provider token prices are quoted in USD per million tokens. That is a cost
# input, not the wallet unit, so the rate card below stays authored in USD --
# it is how the provider docs read, and it makes a price update mechanical --
# and is converted through the ``usd_kes_rate`` setting when a cost is computed.
#
# Costs accumulate in MICRO-CREDITS (1e-6 credit) and are rounded to whole
# credits once, when a reservation settles. Rounding per line would floor every
# LLM call at a whole shilling: a five-call turn costing ~1.4 credits would be
# charged 15.

MICRO_PER_CREDIT: Final[int] = 1_000_000


def _setting_int(key: str, *, default: int) -> int:
    """
    Read an integer setting from ``billing.price_list``.

    Args:
        key: Setting key.
        default: Value when the row is missing or unreadable.

    Returns:
        int: Setting value.
    """
    from dermascan.billing import pricing

    try:
        return int(pricing.setting(key, default=Decimal(default)))
    except Exception:
        logger.exception("price_list setting unreadable, using default: %s", key)
        return default


def usd_kes_rate() -> Decimal:
    """
    Shillings per US dollar, used to price the USD rate card in credits.

    Returns:
        Decimal: Conversion rate from ``billing.price_list``.
    """
    from dermascan.billing import pricing

    try:
        return pricing.setting("usd_kes_rate", default=Decimal(129))
    except Exception:
        logger.exception("usd_kes_rate unreadable, using 129")
        return Decimal(129)


# --- Hold ceilings ---------------------------------------------------------
#
# These are ceilings, not charges: a hold debits the balance immediately and
# the unused remainder is refunded at settle. They live in price_list so they
# are editable from the admin portal, and the shipped values are interim --
# size them from the p99 of billing.usage_line after a metering run.


def orchestrator_hold_credits() -> int:
    """
    Credit ceiling held for one orchestrator turn.

    Returns:
        int: Hold size in credits.
    """
    return max(1, _setting_int("orchestrator_hold_credits", default=25))


def medgemma_hold_credits() -> int:
    """
    Credit ceiling held for one MedGemma inference job.

    Returns:
        int: Hold size in credits.
    """
    return max(1, _setting_int("medgemma_hold_credits", default=60))


def min_balance_credits() -> int:
    """
    Balance a user must retain on top of a hold.

    Zero by design. A hold already debits the balance and guarantees the funds;
    a floor on top of it is a second gate, and a non-zero one strands credits
    the user paid for and can never spend.

    Returns:
        int: Minimum retained balance in credits.
    """
    return max(0, _setting_int("min_balance_credits", default=0))


def reservation_ttl_sec() -> int:
    """
    How long a hold survives before the sweeper releases it.

    Returns:
        int: Time to live in seconds.
    """
    return max(60, _setting_int("reservation_ttl_sec", default=900))


# --- Rate card (USD per 1M tokens) ---------------------------------------------

BILLING_RATE_CARD_VERSION: Final[str] = os.getenv(
    "BILLING_RATE_CARD_VERSION", "2026-08-25"
).strip()

# Paid tier, standard context, USD per 1M tokens.
#   OpenAI: https://developers.openai.com/api/docs/pricing (Aug 2026)
#   Gemini: https://ai.google.dev/gemini-api/docs/pricing (Aug 2026)
BILLING_TOKEN_RATES_USD: Final[dict[str, dict[str, Decimal]]] = {
    "google:gemini-3.5-flash": {
        "input": Decimal("1.50"),
        "output": Decimal("9.00"),
        "cached_input": Decimal("0.15"),
    },
    "google:gemini-3.1-flash-lite": {
        "input": Decimal("0.25"),
        "output": Decimal("1.50"),
        "cached_input": Decimal("0.025"),
    },
    "openai:gpt-4o": {
        "input": Decimal("2.50"),
        "output": Decimal("10.00"),
        "cached_input": Decimal("1.25"),
    },
    "openai:gpt-5": {
        "input": Decimal("1.25"),
        "output": Decimal("10.00"),
        "cached_input": Decimal("0.125"),
    },
    # Summarization chain fallback
    "openai:gpt-4o-mini": {
        "input": Decimal("0.15"),
        "output": Decimal("0.60"),
        "cached_input": Decimal("0.075"),
    },
}


def micro_credits_per_1m(provider_model: str, bucket: str) -> int:
    """
    Price one million tokens in micro-credits.

    Args:
        provider_model: Rate-card key such as ``google:gemini-3.5-flash``.
        bucket: ``input``, ``output`` or ``cached_input``.

    Returns:
        int: Micro-credits per 1M tokens, 0 when the model is not on the card.
    """
    rates = BILLING_TOKEN_RATES_USD.get(provider_model)
    if rates is None:
        return 0
    usd = rates.get(bucket)
    if usd is None or usd <= 0:
        return 0
    return int(usd * usd_kes_rate() * MICRO_PER_CREDIT)


def micro_credits_for_tokens(
    *,
    provider_model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> int:
    """
    Cost of one LLM call in micro-credits.

    No rounding happens here. The scope total is rounded to whole credits once,
    at settle, so a turn made of many small calls is not charged a shilling per
    call.

    Args:
        provider_model: Key such as ``openai:gpt-4o``.
        input_tokens: Non-cached prompt tokens.
        output_tokens: Completion tokens.
        cached_input_tokens: Cached prompt tokens, priced separately.

    Returns:
        int: Non-negative cost in micro-credits.
    """
    if provider_model not in BILLING_TOKEN_RATES_USD:
        return 0

    def _bucket(count: int, bucket: str) -> int:
        if count <= 0:
            return 0
        return (count * micro_credits_per_1m(provider_model, bucket)) // 1_000_000

    return (
        _bucket(max(0, input_tokens), "input")
        + _bucket(max(0, output_tokens), "output")
        + _bucket(max(0, cached_input_tokens), "cached_input")
    )


def micro_to_credits(micro: int) -> int:
    """
    Round a micro-credit total to whole credits.

    Rounds up, so a scope that consumed anything at all is never free.

    Args:
        micro: Cost in micro-credits.

    Returns:
        int: Non-negative whole credits.
    """
    if micro <= 0:
        return 0
    return max(1, (micro + MICRO_PER_CREDIT - 1) // MICRO_PER_CREDIT)


def credits_for_medgemma_job(job_type: str) -> int:
    """
    Flat credit price of one successful MedGemma inference job.

    Args:
        job_type: :class:`BillingJobType` value.

    Returns:
        int: Non-negative credits, from ``billing.price_list``.
    """
    from dermascan.billing import pricing

    try:
        return max(0, pricing.job_credits(job_type))
    except Exception:
        logger.exception("job price unreadable: %s", job_type)
        return 0


__all__ = [
    "BILLING_RATE_CARD_VERSION",
    "BILLING_TOKEN_RATES_USD",
    "BillingJobType",
    "BillingRefType",
    "LedgerKind",
    "LlmPurpose",
    "MICRO_PER_CREDIT",
    "MeterType",
    "ReservationState",
    "credits_for_medgemma_job",
    "medgemma_hold_credits",
    "micro_credits_for_tokens",
    "micro_credits_per_1m",
    "micro_to_credits",
    "min_balance_credits",
    "orchestrator_hold_credits",
    "reservation_ttl_sec",
    "resolve_medgemma_job_type",
    "usd_kes_rate",
]

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
Licence: PolyForm Noncommercial License 1.0.0

Orchestrator tools for credits and top-ups.

Both tools return status dictionaries rather than raising, matching the
``under_pressure`` convention already used for MedGemma backpressure: the model
turns the status into copy, so a payment problem reads as a conversation rather
than an error.

``create_topup`` takes an optional M-PESA number. It is only ever populated
from what the user typed in conversation — no code path extracts a number from
a Telegram or WhatsApp payload. Without one, the user gets a checkout link,
which works for everybody.
"""


from __future__ import annotations

from typing import Annotated, Any, Optional

from langchain_core.tools import tool
from pydantic import Field

from dermascan.billing import accounts, notify, pricing, topups
from dermascan.billing.flags import billing_enabled
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

BillingClientIdArg = Annotated[
    str,
    Field(
        description=(
            "Required. Exact client_id from the orchestrator input "
            "(copy the value shown after 'client_id:'). "
            "Do not use request_id or case_id."
        ),
    ),
]

TopupAmountArg = Annotated[
    int,
    Field(
        description=(
            "Amount to top up, in credits. 1 credit = KES 1.00, so 100 means KES 100. "
            "Use the amount the user asked for. Minimum is 100."
        ),
    ),
]

MsisdnArg = Annotated[
    Optional[str],
    Field(
        default=None,
        description=(
            "Optional. The user's M-PESA number, ONLY if they typed it in this "
            "conversation, in the form +254XXXXXXXXX. Never guess it, never take it "
            "from anywhere other than what the user actually wrote. Leave empty and "
            "they will get a payment link instead."
        ),
    ),
]


def _resolve_account(client_id: str) -> Optional[accounts.Account]:
    """
    Find the billing account for a client.

    Args:
        client_id: Channel-native identifier.

    Returns:
        Optional[Account]: The account, or None when the channel is unknown.
    """
    channel = accounts.resolve_channel(client_id)
    if channel is None:
        logger.warning("cannot resolve channel for client_id=%s", client_id)
        return None
    return accounts.resolve_for_client(channel, client_id)


@tool
def get_credit_balance(client_id: BillingClientIdArg) -> dict[str, Any]:
    """
    Check how many credits this user has left.

    Use this when the user asks about their balance, credits, or what a
    service costs. Do NOT call it before every action — the system tells you
    when a user is out of credits.

    Returns:
        A dict with the balance, whether the free trial was used, and the
        credit cost of each service.
    """
    logger.info("TOOL CALL: get_credit_balance client_id=%s", client_id)

    if not billing_enabled():
        return {"status": "billing_disabled", "instruction": "Credits are not in use. Continue normally."}

    account = _resolve_account(client_id)
    if account is None:
        return {
            "status": "unknown_account",
            "instruction": "Could not look up their balance. Apologise briefly and carry on.",
        }

    return {
        "status": "ok",
        "balance_credits": account.balance_credits,
        "free_trial_used": account.free_credits_granted,
        "prices": {
            "skin_audit": pricing.job_credits("skin_audit"),
            "product_review": pricing.job_credits("product_review_image"),
            "specialist_escalation": pricing.job_credits("specialist_escalation"),
        },
        "instruction": (
            "Tell the user their balance in credits, and mention that 1 credit is KES 1. "
            "Only list prices if they asked what something costs."
        ),
    }


@tool
def create_topup(
    client_id: BillingClientIdArg,
    amount_credits: TopupAmountArg,
    msisdn: MsisdnArg = None,
) -> dict[str, Any]:
    """
    Start a credit top-up for this user.

    Call this when the user wants to add credits. If they gave you their
    M-PESA number in the conversation, pass it as msisdn and they get a payment
    prompt on their phone. If they did not, leave msisdn empty and they get a
    payment link that works with card or M-PESA.

    Never invent an M-PESA number. Only pass one the user actually typed.

    Returns:
        A dict describing what was started and what to tell the user.
    """
    logger.info(
        "TOOL CALL: create_topup client_id=%s amount=%s with_msisdn=%s",
        client_id, amount_credits, bool(msisdn),
    )

    if not billing_enabled():
        return {"status": "billing_disabled", "instruction": "Credits are not in use. Continue normally."}

    account = _resolve_account(client_id)
    if account is None:
        return {
            "status": "unknown_account",
            "instruction": "Could not start a top-up. Apologise briefly and offer to try again.",
        }

    try:
        offer = topups.start_topup(
            account=account, credits=amount_credits, msisdn=(msisdn or None)
        )
    except ValueError as exc:
        return {
            "status": "invalid_amount",
            "minimum_credits": pricing.min_topup_credits(),
            "instruction": f"Tell the user: {exc}. Ask what amount they'd like.",
        }
    except Exception:
        logger.exception("topup failed: client_id=%s", client_id)
        return {
            "status": "topup_failed",
            "instruction": (
                "Top-up could not be started right now. Apologise and suggest trying again "
                "shortly. Do NOT retry this tool in this turn."
            ),
        }

    if offer.stk_sent:
        return {
            "status": "stk_sent",
            "credits": offer.credits,
            "reference": offer.reference,
            "instruction": notify.stk_sent_message(credits=offer.credits, msisdn=msisdn or ""),
        }

    fallback_note = (
        " The phone prompt didn't go through, so send them the link instead."
        if offer.fallback_reason
        else ""
    )
    return {
        "status": "checkout_link",
        "credits": offer.credits,
        "reference": offer.reference,
        "authorization_url": offer.authorization_url,
        "instruction": (
            f"Give the user this payment link exactly as it is: {offer.authorization_url} "
            f"— it covers card and M-PESA. Tell them credits land automatically once they pay."
            f"{fallback_note}"
        ),
    }

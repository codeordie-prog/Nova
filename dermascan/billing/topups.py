"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Top-up orchestration: the two ways a user can add credits.

1. **A checkout link.** Always available, needs nothing from the user. The
   reference embedded in it means however they pay inside that page — card or
   mobile money — the credit lands on their account.
2. **An STK prompt.** Only when the user has volunteered an M-PESA number in
   conversation. No code here reads a number out of a channel payload; the
   number arrives as a tool argument or not at all.

STK is offered first because it keeps the user in the chat, and it falls back
to the link whenever Paystack rejects the charge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from dermascan.billing import accounts, intents, paystack_client, pricing
from dermascan.billing.flags import paystack_callback_url
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TopupOffer:
    """
    What to tell the user after starting a top-up.

    Attributes:
        reference: Tracking reference for this attempt.
        credits: Credits that will land once payment verifies.
        method: ``checkout`` or ``stk``.
        authorization_url: Link to send, for checkout.
        stk_sent: True when an STK prompt is on its way to the user's phone.
        fallback_reason: Why STK was not used, when it was requested but skipped.
    """

    reference: str
    credits: int
    method: str
    authorization_url: Optional[str]
    stk_sent: bool
    fallback_reason: Optional[str]


def _metadata(account: accounts.Account, credits: int) -> dict[str, Any]:
    """
    Context echoed back by Paystack on the webhook.

    Args:
        account: The paying account.
        credits: Credits being purchased.

    Returns:
        dict[str, Any]: Metadata payload.
    """
    return {
        "account_id": account.id,
        "client_id": account.client_id,
        "channel": account.channel,
        "credits": credits,
    }


def start_topup(
    *,
    account: accounts.Account,
    credits: int,
    msisdn: Optional[str] = None,
) -> TopupOffer:
    """
    Begin a top-up, preferring STK when a number was supplied.

    Args:
        account: Account being topped up.
        credits: Credits to buy (1 credit = KES 1.00).
        msisdn: M-PESA number the user volunteered, if any.

    Returns:
        TopupOffer: What the chat surface should tell the user.

    Raises:
        ValueError: When the amount is below the configured minimum.
    """
    minimum = pricing.min_topup_credits()
    if credits < minimum:
        raise ValueError(f"minimum top-up is {minimum} credits")

    amount_minor = paystack_client.credits_to_minor(credits)
    reference = paystack_client.new_reference(account.id)
    email = paystack_client.billing_email(account.id, account.email)
    metadata = _metadata(account, credits)

    if msisdn:
        accounts.set_contact(account.id, msisdn=msisdn)
        intents.create(
            account_id=account.id,
            reference=reference,
            amount_minor=amount_minor,
            currency=account.currency,
            credits_to_grant=credits,
            method=intents.METHOD_STK,
            msisdn=msisdn,
        )
        try:
            result = paystack_client.charge_mobile_money(
                email=email,
                amount_minor=amount_minor,
                currency=account.currency,
                reference=reference,
                msisdn=msisdn,
                metadata=metadata,
            )
        except Exception as exc:
            # The user still needs a way to pay, so fall through to a link
            # rather than surfacing a failure they cannot act on.
            logger.warning("stk charge failed, falling back to checkout: %s", exc)
            intents.update_status(reference, intents.STATUS_FAILED)
            return _start_checkout(
                account=account,
                credits=credits,
                fallback_reason="stk_failed",
            )

        intents.update_status(
            reference, intents.STATUS_PENDING, channel_used="mobile_money", raw=result.raw
        )
        return TopupOffer(
            reference=reference,
            credits=credits,
            method=intents.METHOD_STK,
            authorization_url=None,
            stk_sent=True,
            fallback_reason=None,
        )

    return _start_checkout(account=account, credits=credits, fallback_reason=None)


def _start_checkout(
    *,
    account: accounts.Account,
    credits: int,
    fallback_reason: Optional[str],
) -> TopupOffer:
    """
    Create a hosted checkout link.

    Args:
        account: Account being topped up.
        credits: Credits to buy.
        fallback_reason: Set when this follows a failed STK attempt.

    Returns:
        TopupOffer: Offer containing the authorization URL.
    """
    amount_minor = paystack_client.credits_to_minor(credits)
    reference = paystack_client.new_reference(account.id)
    intents.create(
        account_id=account.id,
        reference=reference,
        amount_minor=amount_minor,
        currency=account.currency,
        credits_to_grant=credits,
        method=intents.METHOD_CHECKOUT,
    )
    result = paystack_client.initialize_transaction(
        email=paystack_client.billing_email(account.id, account.email),
        amount_minor=amount_minor,
        currency=account.currency,
        reference=reference,
        callback_url=paystack_callback_url(),
        metadata=_metadata(account, credits),
    )
    intents.update_status(
        reference,
        intents.STATUS_PENDING,
        authorization_url=result.authorization_url,
        raw=result.raw,
    )
    return TopupOffer(
        reference=reference,
        credits=credits,
        method=intents.METHOD_CHECKOUT,
        authorization_url=result.authorization_url,
        stk_sent=False,
        fallback_reason=fallback_reason,
    )


def offer_for_blocked_request(
    *,
    account: accounts.Account,
    balance: int,
    required: Optional[int] = None,
) -> str:
    """
    Build the message shown when a request is turned away for want of credits.

    Creates a checkout link up front so the user has a route that asks nothing
    of them, and falls back to the link-less copy if Paystack is unreachable —
    being told how to pay is better than being told nothing.

    Args:
        account: Account that was blocked.
        balance: Their current balance.
        required: Credits the blocked action needed, when known.

    Returns:
        str: Message text offering both top-up routes.
    """
    from dermascan.billing import notify

    authorization_url: Optional[str] = None
    try:
        offer = start_topup(account=account, credits=pricing.min_topup_credits())
        authorization_url = offer.authorization_url
    except Exception:
        logger.exception("could not create a top-up link: account_id=%s", account.id)

    return notify.out_of_credits_message(
        authorization_url=authorization_url,
        balance=balance,
        required=required,
    )

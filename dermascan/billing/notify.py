"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-20
Licence: PolyForm Noncommercial License 1.0.0

Billing messages to users, and the copy for them.

The orchestrator's ``_enqueue_outbound`` is private to that worker, so billing
produces onto the same ``outbound:messages`` stream itself. Delivery, ordering,
chunking and channel dispatch stay the outbound sender's job.

All copy is plain text. There are no buttons or inline keyboards anywhere in
this flow: the Telegram outbound path sends ``{chat_id, text}`` only, and a
link in a message works on every channel without new plumbing.
"""


from __future__ import annotations

import os
import time
from typing import Final, Optional

from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client
from dermascan.shared.stream_pipeline_tracing import enrich_stream_fields

logger = get_logger(__name__)

OUTBOUND_STREAM: Final[str] = os.getenv("OUTBOUND_STREAM", "outbound:messages")
WORKERS_STREAM_MAXLEN: Final[int] = int(os.getenv("WORKERS_STREAM_MAXLEN", "5000"))

KIND_BILLING: Final[str] = "billing_update"


def enqueue(
    *,
    client_id: str,
    channel: str,
    text: str,
    request_id: str,
    case_id: str = "",
    dedupe_discriminator: str = "",
) -> None:
    """
    Put a billing message on the outbound stream.

    Args:
        client_id: Recipient, as the outbound sender addresses them.
        channel: ``whatsapp`` or ``telegram``.
        text: Message body.
        request_id: Correlation id for tracing.
        case_id: Case id when the message relates to one.
        dedupe_discriminator: Extra token so two billing messages for one
            request are not collapsed as duplicates.
    """
    body = text.strip()
    if not body:
        return

    client = get_redis_client(decode_responses=True)
    sequence = client.incr(f"outbound:seq:{client_id}")
    fields = enrich_stream_fields(
        {
            "client_id": client_id,
            "case_id": case_id,
            "channel": channel,
            "request_id": request_id,
            "kind": KIND_BILLING,
            "must_deliver": "true",
            "priority": "10",
            "created_at_ms": str(int(time.time() * 1000)),
            "seq": str(sequence),
            "text": body,
            "dedupe_key": f"{KIND_BILLING}:{dedupe_discriminator}:{request_id}:{case_id}",
            "reply_to_message_id": "",
        }
    )
    client.xadd(OUTBOUND_STREAM, fields, maxlen=WORKERS_STREAM_MAXLEN, approximate=True)
    logger.info("billing message queued: client_id=%s kind=%s", client_id, KIND_BILLING)


def out_of_credits_message(
    *, authorization_url: Optional[str], balance: int, required: Optional[int] = None
) -> str:
    """
    Copy for a user who cannot pay for what they asked.

    Offers both routes at once: a link that needs nothing from them, and an
    STK prompt if they would rather stay in the chat. The link always works,
    so a user who does not want to share a number is never stuck.

    Args:
        authorization_url: Checkout link, when one could be created.
        balance: Their current balance.
        required: Credits the blocked action needed, when known.

    Returns:
        str: Message text.
    """
    if required is not None:
        opening = (
            f"You need {required} credits for that and you have {balance}. "
            "Here are two ways to top up:"
        )
    else:
        opening = "You've run out of credits. Two ways to top up:"

    if authorization_url:
        route_one = f"1. Pay here — {authorization_url} — card or M-PESA, whichever you prefer."
    else:
        route_one = "1. Tell me the amount and I'll send you a payment link."

    route_two = (
        "2. Or send me your M-PESA number and the amount, "
        "and I'll push a payment prompt to your phone."
    )
    return f"{opening}\n\n{route_one}\n{route_two}"


def stk_sent_message(*, credits: int, msisdn: str) -> str:
    """
    Copy for an STK prompt on its way.

    Args:
        credits: Credits being bought.
        msisdn: Number the prompt went to.

    Returns:
        str: Message text.
    """
    return (
        f"Sent a payment request for KES {credits} to {msisdn}. "
        "Check your phone and enter your M-PESA PIN to confirm."
    )


def payment_confirmed_message(
    *, credits: int, balance: int, pending_task: Optional[str] = None
) -> str:
    """
    Copy for a confirmed payment, offering to resume any blocked work.

    The resume is offered, not performed: auto-continuing would spend the
    credits they just bought on something they may no longer want.

    Args:
        credits: Credits added.
        balance: Balance after the top-up.
        pending_task: What they were blocked on, if anything.

    Returns:
        str: Message text.
    """
    confirmation = (
        f"Payment confirmed — KES {credits} received. "
        f"Your balance is now {balance} credits."
    )
    if pending_task:
        return f"{confirmation}\n\nYou had {pending_task} waiting. Want me to continue with it?"
    return f"{confirmation}\n\nWhat would you like to do next?"


def payment_failed_message() -> str:
    """
    Copy for a payment that did not complete.

    Returns:
        str: Message text.
    """
    return (
        "That payment didn't go through, so you haven't been charged. "
        "Tell me the amount if you'd like to try again."
    )

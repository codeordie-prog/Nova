"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Token usage lines (balance-neutral until reservation settle).
"""


from __future__ import annotations

import json
from typing import Any, Optional

from psycopg import Connection

from dermascan.billing.config import (
    BILLING_RATE_CARD_VERSION,
    LlmPurpose,
    MICRO_PER_CREDIT,
    MeterType,
    micro_credits_for_tokens,
    micro_to_credits,
)
from dermascan.billing.db import transaction
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def sum_usage_micro_for_scope(conn: Connection, scope_key: str) -> int:
    """
    Sum the micro-credit cost of every usage line on a scope key.

    Micro, not credits: summing the rounded per-line figures would multiply the
    rounding error by the number of LLM calls in the turn.

    Args:
        conn: Open connection inside a transaction.
        scope_key: Correlation key (e.g. ``request_id``).

    Returns:
        int: Total cost for the scope in micro-credits.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_micro_credits), 0)::bigint AS total
        FROM billing.usage_line
        WHERE scope_key = %s
        """,
        (scope_key,),
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def record_usage_line(
    *,
    account_id: str,
    scope_key: str,
    line_seq: int,
    provider_model: str,
    input_tokens: int,
    output_tokens: int,
    purpose: LlmPurpose,
    reservation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """
    Append one meter line for an LLM call.

    Args:
        account_id: Account uuid.
        scope_key: Correlation key (usually ``request_id``).
        line_seq: Monotonic sequence within ``scope_key``.
        provider_model: Rate-card key such as ``google:gemini-3.5-flash``.
        input_tokens: Prompt token count.
        output_tokens: Completion token count.
        purpose: LLM call purpose.
        reservation_id: Optional linked reservation uuid.
        metadata: Optional extra context.

    Returns:
        int: ``cost_credits`` recorded for this line (display value; the
            authoritative figure is ``cost_micro_credits``).
    """
    provider, _, model = provider_model.partition(":")
    cost_micro = micro_credits_for_tokens(
        provider_model=provider_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    cost = micro_to_credits(cost_micro)
    idempotency_key = f"usage:{scope_key}:{line_seq}"
    meta = {"rate_card": BILLING_RATE_CARD_VERSION, **(metadata or {})}

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO billing.usage_line (
                account_id, reservation_id, scope_key, line_seq, meter_type,
                provider, model, purpose, input_tokens, output_tokens,
                cost_micro_credits, cost_credits, idempotency_key, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                account_id,
                reservation_id,
                scope_key,
                line_seq,
                MeterType.LLM.value,
                provider or None,
                model or None,
                purpose.value,
                max(0, input_tokens),
                max(0, output_tokens),
                cost_micro,
                cost,
                idempotency_key,
                _json(meta),
            ),
        )

    logger.info(
        "billing usage line: scope=%s seq=%s model=%s in=%s out=%s micro=%s "
        "credits=%s reservation_id=%s",
        scope_key,
        line_seq,
        provider_model,
        input_tokens,
        output_tokens,
        cost_micro,
        cost,
        reservation_id,
    )
    return cost


def record_job_usage_line(
    *,
    account_id: str,
    scope_key: str,
    line_seq: int,
    job_type: str,
    cost_credits: int,
    idempotency_key: str,
    reservation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """
    Append one flat-credit usage line for a job that consumes no tokens.

    For work the token meter cannot see -- a specialist review done by a human,
    for instance. The caller supplies ``idempotency_key`` rather than a line
    sequence, because these charges are deduplicated by the thing being paid
    for, not by their position in a scope.

    Args:
        account_id: Account uuid.
        scope_key: Billing scope key, usually the turn's ``request_id``.
        line_seq: Position within the scope. ``(scope_key, line_seq)`` is
            unique, so this must come from the scope's counter even though
            deduplication is done on ``idempotency_key``.
        job_type: Billable job type label.
        cost_credits: Flat SKU credits for the job.
        idempotency_key: Unique key for this charge.
        reservation_id: Optional linked reservation uuid.
        metadata: Optional extra context.

    Returns:
        int: Credits recorded, or 0 when the charge was a duplicate.
    """
    cost = max(0, int(cost_credits))
    meta = {
        "rate_card": BILLING_RATE_CARD_VERSION,
        "job_type": job_type,
        **(metadata or {}),
    }

    with transaction() as conn:
        inserted = conn.execute(
            """
            INSERT INTO billing.usage_line (
                account_id, reservation_id, scope_key, line_seq, meter_type,
                provider, model, purpose, input_tokens, output_tokens,
                cost_micro_credits, cost_credits, idempotency_key, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                account_id,
                reservation_id,
                scope_key,
                line_seq,
                MeterType.TOOL.value,
                None,
                None,
                job_type,
                0,
                0,
                cost * MICRO_PER_CREDIT,
                cost,
                idempotency_key,
                _json(meta),
            ),
        ).fetchone()

    if inserted is None:
        logger.info(
            "billing job charge skipped as duplicate: scope=%s job_type=%s key=%s",
            scope_key,
            job_type,
            idempotency_key,
        )
        return 0

    logger.info(
        "billing job charge: scope=%s job_type=%s credits=%s reservation_id=%s",
        scope_key,
        job_type,
        cost,
        reservation_id,
    )
    return cost


def record_inference_usage_line(
    *,
    account_id: str,
    scope_key: str,
    job_type: str,
    cost_credits: int,
    reservation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """
    Append one flat-credit usage line for a MedGemma inference job.

    Args:
        account_id: Account uuid.
        scope_key: MedGemma ``job_id``.
        job_type: Billable job type label.
        cost_credits: Flat SKU credits for the job.
        reservation_id: Optional linked reservation uuid.
        metadata: Optional extra context.

    Returns:
        int: ``cost_credits`` recorded for this line (display value; the
            authoritative figure is ``cost_micro_credits``).
    """
    cost = max(0, int(cost_credits))
    idempotency_key = f"usage:{scope_key}:1"
    meta = {
        "rate_card": BILLING_RATE_CARD_VERSION,
        "job_type": job_type,
        **(metadata or {}),
    }

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO billing.usage_line (
                account_id, reservation_id, scope_key, line_seq, meter_type,
                provider, model, purpose, input_tokens, output_tokens,
                cost_micro_credits, cost_credits, idempotency_key, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                account_id,
                reservation_id,
                scope_key,
                1,
                MeterType.INFERENCE.value,
                "medgemma",
                "medgemma-4b",
                job_type,
                0,
                0,
                cost * MICRO_PER_CREDIT,
                cost,
                idempotency_key,
                _json(meta),
            ),
        )
    return cost

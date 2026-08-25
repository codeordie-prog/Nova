"""
Token usage lines (balance-neutral until reservation settle).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from psycopg import Connection

from dermascan.billing.config import (
    BILLING_RATE_CARD_VERSION,
    LlmPurpose,
    MeterType,
    credits_for_tokens,
)
from dermascan.billing.db import transaction


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def sum_usage_for_scope(conn: Connection, scope_key: str) -> int:
    """
    Sum ``cost_credits`` for all usage lines on a scope key.

    Args:
        conn: Open connection inside a transaction.
        scope_key: Correlation key (e.g. ``request_id``).

    Returns:
        int: Total credited cost for the scope.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_credits), 0)::bigint AS total
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
        int: ``cost_credits`` recorded for this line.
    """
    provider, _, model = provider_model.partition(":")
    cost = credits_for_tokens(
        provider_model=provider_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    idempotency_key = f"usage:{scope_key}:{line_seq}"
    meta = {"rate_card": BILLING_RATE_CARD_VERSION, **(metadata or {})}

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO billing.usage_line (
                account_id, reservation_id, scope_key, line_seq, meter_type,
                provider, model, purpose, input_tokens, output_tokens,
                cost_credits, idempotency_key, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
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
                cost,
                idempotency_key,
                _json(meta),
            ),
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
        int: ``cost_credits`` recorded for this line.
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
                cost_credits, idempotency_key, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
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
                cost,
                idempotency_key,
                _json(meta),
            ),
        )
    return cost

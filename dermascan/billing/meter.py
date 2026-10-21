"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-21
License: MIT

LLM usage extraction and emit-only metering for the model fallback chain.
"""


from __future__ import annotations

from typing import Any, Union

from langchain.agents.middleware import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage

from dermascan.billing.config import LlmPurpose
from dermascan.billing.context import billing_scope
from dermascan.billing.usage import record_job_usage_line, record_usage_line
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


def chain_name_to_provider_model(model_key: str) -> str:
    """
    Map model-chain registry keys to rate-card keys.

    Args:
        model_key: Chain name such as ``gemini:gemini-3.5-flash``.

    Returns:
        str: Rate-card key such as ``google:gemini-3.5-flash`` or ``openai:gpt-4o``.
    """
    if model_key.startswith("gemini:"):
        return f"google:{model_key.split(':', 1)[1]}"
    return model_key


def extract_token_usage(
    response: Union[ModelResponse, AIMessage, ExtendedModelResponse, Any],
) -> tuple[int, int]:
    """
    Extract input/output token counts from a model response.

    Args:
        response: Middleware or chat model result.

    Returns:
        tuple[int, int]: ``(input_tokens, output_tokens)``, zeros when unknown.
    """
    messages: list[BaseMessage] = []

    if isinstance(response, AIMessage):
        messages = [response]
    elif isinstance(response, ModelResponse):
        messages = list(response.result)
    elif isinstance(response, ExtendedModelResponse):
        inner = response.model_response
        if isinstance(inner, AIMessage):
            messages = [inner]
        elif isinstance(inner, ModelResponse):
            messages = list(inner.result)

    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        meta = getattr(message, "usage_metadata", None) or {}
        if not isinstance(meta, dict):
            continue
        input_tokens = int(
            meta.get("input_tokens")
            or meta.get("prompt_tokens")
            or 0
        )
        output_tokens = int(
            meta.get("output_tokens")
            or meta.get("completion_tokens")
            or 0
        )
        if input_tokens or output_tokens:
            return input_tokens, output_tokens

    return 0, 0


def purpose_from_operation(operation: str) -> LlmPurpose:
    """
    Map model-chain operation labels to billing purpose.

    Args:
        operation: Label passed to :class:`ModelChainRunner`.

    Returns:
        LlmPurpose: Summarization or agent step.
    """
    if operation.strip().lower() == "summarization":
        return LlmPurpose.SUMMARIZATION
    return LlmPurpose.AGENT_STEP


def record_llm_usage(
    *,
    model_key: str,
    response: Any,
    operation: str = "agent_step",
) -> None:
    """
    Emit one ``usage_line`` when a billing scope is active.

    Args:
        model_key: Successful model chain entry name.
        response: Model response containing usage metadata.
        operation: Operation label for purpose mapping.
    """
    scope = billing_scope.get()
    if scope is None:
        return

    input_tokens, output_tokens = extract_token_usage(response)
    if input_tokens == 0 and output_tokens == 0:
        return

    provider_model = chain_name_to_provider_model(model_key)
    purpose = purpose_from_operation(operation)
    line_seq = scope.next_line_seq()

    try:
        cost = record_usage_line(
            account_id=scope.account_id,
            scope_key=scope.scope_key,
            line_seq=line_seq,
            provider_model=provider_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            purpose=purpose,
            reservation_id=scope.reservation_id,
            metadata={"model_key": model_key, "operation": operation},
        )
        logger.debug(
            "billing usage line queued: scope=%s seq=%s model=%s cost_credits=%s "
            "in=%s out=%s",
            scope.scope_key,
            line_seq,
            provider_model,
            cost,
            input_tokens,
            output_tokens,
        )
    except Exception:
        logger.exception(
            "billing usage line failed: scope=%s model=%s",
            scope.scope_key,
            provider_model,
        )


def record_job_charge(job_type: str, *, dedupe_on: str) -> int:
    """
    Charge a flat job SKU into the active billing scope.

    For work that costs money but produces no tokens, so the LLM meter never
    sees it. Settles with the rest of the scope, which means it can push a turn
    past its hold and be collected as an overrun rather than silently dropped.

    Args:
        job_type: ``billing.price_list`` key under scope ``job_credits``.
        dedupe_on: Stable identifier for the thing being paid for, so a retried
            or repeated tool call charges once.

    Returns:
        int: Credits charged. 0 when no scope is active, the job is free, or
            the charge was a duplicate.
    """
    scope = billing_scope.get()
    if scope is None:
        return 0

    from dermascan.billing import pricing

    try:
        credits = max(0, pricing.job_credits(job_type))
    except Exception:
        logger.exception("job price unreadable: %s", job_type)
        return 0

    if credits <= 0:
        return 0

    try:
        return record_job_usage_line(
            account_id=scope.account_id,
            scope_key=scope.scope_key,
            line_seq=scope.next_line_seq(),
            job_type=job_type,
            cost_credits=credits,
            idempotency_key=f"job:{job_type}:{dedupe_on}",
            reservation_id=scope.reservation_id,
        )
    except Exception:
        logger.exception(
            "billing job charge failed: job_type=%s dedupe_on=%s", job_type, dedupe_on
        )
        return 0


def job_credits_shortfall(job_type: str) -> int:
    """
    Credits a caller is short of affording a flat job, 0 when it can pay.

    A flat SKU dearer than the turn's hold always overruns, and an overrun is
    collected after the work is done -- which for human review means the
    dermatologist has already been booked. Callers that dispatch real-world
    cost check this first.

    Returns 0 whenever enforcement is off, no scope is active, or the job is
    free, so shadow-mode runs are never blocked.

    Args:
        job_type: ``billing.price_list`` key under scope ``job_credits``.

    Returns:
        int: Missing credits, or 0 when the charge is affordable.
    """
    from dermascan.billing import accounts, pricing
    from dermascan.billing.flags import billing_enforce_enabled

    if not billing_enforce_enabled():
        return 0

    scope = billing_scope.get()
    if scope is None:
        return 0

    try:
        credits = max(0, pricing.job_credits(job_type))
        if credits <= 0:
            return 0
        # The hold has already been debited, so the balance read here is what
        # is left after it -- money the turn has not spent yet.
        balance = accounts.get_balance(scope.account_id) + scope_hold_credits(scope)
        return max(0, credits - balance)
    except Exception:
        logger.exception("job affordability check failed: %s", job_type)
        return 0


def scope_hold_credits(scope: Any) -> int:
    """
    Credits still held by this scope's reservation, 0 when there is none.

    The hold was debited from the balance when it was placed, so it has to be
    added back to see what the turn can actually spend.

    Args:
        scope: Active :class:`BillingScope`.

    Returns:
        int: Held credits available to this scope.
    """
    from dermascan.billing.db import transaction

    if not scope.reservation_id:
        return 0
    with transaction() as conn:
        row = conn.execute(
            "SELECT hold_credits, state FROM billing.reservation WHERE id = %s",
            (scope.reservation_id,),
        ).fetchone()
    if row is None or str(row["state"]) != "held":
        return 0
    return int(row["hold_credits"])

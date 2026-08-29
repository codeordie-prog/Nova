"""
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
from dermascan.billing.usage import record_usage_line
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

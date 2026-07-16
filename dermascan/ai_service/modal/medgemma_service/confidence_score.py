"""
Author: Dermascan Team
Date: 2026-4-14
Licence: MIT

This module computes the diagnosis/product review confidence score of the model.
The logic here is:
IF diagnosis:
1. extract the diagnosis text from the response, 
2. map its logarithmic probabilities, 
3. get the mean of the logarithmic probabilities,
4. return the exponential of the mean - the probability (which is in [0,1]).

ELSE IF Product review:
1. Extract 3 key fields - 'product_name','how_to_use','safety_in_pregnancy'
2. Collect logprobs for tokens that overlap for each field extracted
3. get the mean of the logarithmic probabilities
4. Return the exponential of the mean


The mathematical description is provided in the `confidence_score_algorithm.md` file.

"""

import json
import math
from typing import Any


_PRODUCT_CONFIDENCE_FIELDS: tuple[str, ...] = (
    "product_name",
    "how_to_use",
    "safety_in_pregnancy",
)


def _tokens_for_span(tokens: list[dict[str, Any]], full_text: str, span_start: int, span_end: int) -> list[float]:
    """
    Collect logprobs for tokens whose character span overlaps [span_start, span_end).
    
    Args:
        tokens: List of token dictionaries with 'token' and 'logprob' keys
        full_text: The full text emitted by the model
        span_start: Start index of the diagnosis text in the full text
        span_end: End index of the diagnosis text in the full text
    
    Returns:
        List of logprobs for tokens overlapping the diagnosis span
    """
    logprobs: list[float] = []
    pos = 0
    for tok in tokens:
        token_text: str = tok.get("token") or ""
        tok_start: int = pos
        tok_end: int = pos + len(token_text)
        pos = tok_end

        if tok_end <= span_start:
            continue
        if tok_start >= span_end:
            break

        lp = tok.get("logprob")
        if lp is not None:
            logprobs.append(lp)
    return logprobs


def compute_confidence_score(data: dict) -> float:
    """
    Compute diagnosis confidence from vLLM response data.

    Strategy: parse the JSON content, locate the "diagnosis" string within the
    tokenized output, gather its token logprobs, then return exp(mean_logprob)
    in [0,1]. Falls back to full-output mean if span match fails.

    Args:
        data: vLLM response data dictionary
        
    Returns:
        Confidence score in [0,1]
    """

    # Extract choice
    choices = data.get("choices") or []
    if not choices:
        return 0.0
    choice = choices[0]

    # Parse the JSON content to get the diagnosis text
    diagnosis_text = ""
    parsed: dict[str, Any] = {}
    content = (choice.get("message") or {}).get("content") or ""
    try:
        parsed = json.loads(content)
        diagnosis_text = str(parsed.get("diagnosis") or "").strip()
    except Exception:
        diagnosis_text = ""
        parsed = {}

    # Extract logprobs
    logprobs = choice.get("logprobs") or {}

    # Extract tokens with their logprobs
    tokens = logprobs.get("content") or []
    if not tokens:
        return 0.0

    # Reconstruct the full text emitted by the model
    full_text = "".join((tok.get("token") or "") for tok in tokens)

    # Find the target field spans in the emitted text
    span_logprobs: list[float] = []
    if diagnosis_text:
        start_search = full_text.find("\"diagnosis\"")
        start_idx = full_text.find(diagnosis_text, start_search if start_search != -1 else 0)
        if start_idx != -1:
            end_idx = start_idx + len(diagnosis_text)

            # Extract the logprobs that overlap with the diagnosis text
            span_logprobs = _tokens_for_span(tokens, full_text, start_idx, end_idx)
    else:
        # Product-review case: diagnosis is intentionally empty.
        # Build confidence from product_name + how_to_use + safety_in_pregnancy spans.
        for field_name in _PRODUCT_CONFIDENCE_FIELDS:
            field_text: str = str(parsed.get(field_name) or "").strip()
            if not field_text:
                continue

            start_search = full_text.find(f'"{field_name}"')
            start_idx = full_text.find(field_text, start_search if start_search != -1 else 0)
            if start_idx == -1:
                continue

            end_idx = start_idx + len(field_text)
            span_logprobs.extend(_tokens_for_span(tokens, full_text, start_idx, end_idx))

    # Fallback: use all tokens if we couldn't align the diagnosis span
    if not span_logprobs:
        span_logprobs = [t.get("logprob") for t in tokens if t.get("logprob") is not None]

    if not span_logprobs:
        return 0.0

    # Compute mean logprob over the diagnosis span
    #mean_logprob = sum(span_logprobs) / len(span_logprobs)

    # Convert logprob to probability
    #confidence = math.exp(mean_logprob)

    m = len(span_logprobs)
    from math import prod
    geometric_mean = prod(math.exp(lp) for lp in span_logprobs) ** (1/m)
    confidence = geometric_mean 

    # Clamp to [0,1]
    return max(0.0, min(1.0, confidence))

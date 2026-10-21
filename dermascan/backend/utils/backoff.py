"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: DermaScan Team
Date: 2026-06-10
License: MIT
Description:
    Retry and backoff helpers for outbound HTTP delivery.

    This module provides functions to parse Retry-After headers and sleep for
    exponential backoff with bounded jitter. It also includes a helper function
    to POST with bounded retries for transient outbound delivery failures.

    The module is used by the outbound sender worker to handle retries for
    transient outbound delivery failures.

"""


from __future__ import annotations

import random
import time
from typing import Any

import requests

RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def parse_retry_after_seconds(value: str) -> float | None:
    """
    Parse a Retry-After header into seconds.

    Args:
        value: Raw Retry-After header value.

    Returns:
        Parsed delay in seconds, or None when parsing fails.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def sleep_outbound_backoff(
    attempt: int,
    *,
    base_delay_s: float,
    retry_after_s: float | None = None,
) -> None:
    """
    Sleep before the next outbound HTTP retry attempt.

    Args:
        attempt: Zero-based attempt index for the retry about to run.
        base_delay_s: Base exponential backoff delay in seconds.
        retry_after_s: Optional provider-supplied Retry-After delay.
    """
    if retry_after_s is not None and retry_after_s > 0:
        time.sleep(retry_after_s + random.uniform(0.0, 0.25))
        return
    delay = base_delay_s * (2 ** attempt) + random.uniform(0.0, base_delay_s)
    time.sleep(delay)


def request_with_outbound_retry(
    *,
    method: str,
    url: str,
    max_retries: int,
    base_delay_s: float,
    **request_kwargs: Any,
) -> requests.Response:
    """
    Issue an HTTP request with bounded retries for transient failures.

    Retries connection errors, timeouts, and HTTP 429/5xx responses. Honors
    Retry-After when present on 429 responses.

    Args:
        method: HTTP method, e.g. ``POST`` or ``GET``.
        url: Request URL.
        max_retries: Number of retries after the initial attempt.
        base_delay_s: Base delay for exponential backoff.
        **request_kwargs: Arguments forwarded to ``requests.request``.

    Returns:
        Successful HTTP response.

    Raises:
        requests.RequestException: When all attempts are exhausted.
        RuntimeError: When a non-retryable HTTP error is returned.
    """
    attempts = max(1, max_retries + 1)
    last_exc: Exception | None = None

    for attempt in range(attempts):
        try:
            response = requests.request(method, url, **request_kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                raise
            sleep_outbound_backoff(attempt, base_delay_s=base_delay_s)
            continue

        if response.ok:
            return response

        if response.status_code not in RETRYABLE_HTTP_STATUSES:
            response.raise_for_status()

        last_exc = requests.HTTPError(
            f"{response.status_code} retryable response from {url}",
            response=response,
        )
        if attempt >= attempts - 1:
            response.raise_for_status()

        retry_after_s = None
        if response.status_code == 429:
            retry_after_s = parse_retry_after_seconds(response.headers.get("Retry-After", ""))
        sleep_outbound_backoff(
            attempt,
            base_delay_s=base_delay_s,
            retry_after_s=retry_after_s,
        )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"outbound {method} exhausted retries: url={url}")


def post_with_outbound_retry(
    *,
    url: str,
    max_retries: int,
    base_delay_s: float,
    **request_kwargs: Any,
) -> requests.Response:
    """
    POST with bounded retries for transient outbound delivery failures.

    Args:
        url: Request URL.
        max_retries: Number of retries after the initial attempt.
        base_delay_s: Base delay for exponential backoff.
        **request_kwargs: Arguments forwarded to ``requests.post``.

    Returns:
        Successful HTTP response.
    """
    return request_with_outbound_retry(
        method="POST",
        url=url,
        max_retries=max_retries,
        base_delay_s=base_delay_s,
        **request_kwargs,
    )


def get_with_outbound_retry(
    *,
    url: str,
    max_retries: int,
    base_delay_s: float,
    **request_kwargs: Any,
) -> requests.Response:
    """
    GET with bounded retries. Used for Paystack transaction verification.

    Args:
        url: Request URL.
        max_retries: Number of retries after the initial attempt.
        base_delay_s: Base delay for exponential backoff.
        **request_kwargs: Arguments forwarded to ``requests.get``.

    Returns:
        Successful HTTP response.
    """
    return request_with_outbound_retry(
        method="GET",
        url=url,
        max_retries=max_retries,
        base_delay_s=base_delay_s,
        **request_kwargs,
    )

"""
Thread-safe circuit breaker for model providers.

Prevents runaway retries against an unhealthy provider (e.g. a cold or down
self-hosted Qwen endpoint). After a configurable number of consecutive
failures the circuit opens for a cooldown window, during which callers are told
to skip the provider and fail over immediately. After the cooldown a single
half-open probe is allowed; success closes the circuit, failure re-opens it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Final

_STATE_CLOSED: Final[str] = "closed"
_STATE_OPEN: Final[str] = "open"
_STATE_HALF_OPEN: Final[str] = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """
    Circuit breaker tuning parameters.

    Args:
        failure_threshold: Consecutive failures that trip the circuit open.
        cooldown_s: Seconds the circuit stays open before a half-open probe.
    """

    failure_threshold: int = 3
    cooldown_s: float = 60.0


class CircuitBreaker:
    """
    A minimal, thread-safe circuit breaker for a single provider.

    Attributes:
        name: Provider identifier this breaker guards.
        config: Tuning parameters controlling trip/cooldown behavior.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig) -> None:
        """
        Initialize the circuit breaker.

        Args:
            name: Provider identifier.
            config: Tuning parameters.
        """
        self.name: str = name
        self.config: CircuitBreakerConfig = config
        self._lock: threading.Lock = threading.Lock()
        self._state: str = _STATE_CLOSED
        self._consecutive_failures: int = 0
        self._opened_at_s: float = 0.0

    @property
    def state(self) -> str:
        """Return the current circuit state (closed/open/half_open)."""
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """
        Decide whether a request to the provider should be attempted now.

        Returns:
            bool: True if the call may proceed; False if the circuit is open.
        """
        with self._lock:
            if self._state == _STATE_OPEN:
                elapsed: float = time.monotonic() - self._opened_at_s
                if elapsed >= self.config.cooldown_s:
                    # Allow a single probe to test recovery.
                    self._state = _STATE_HALF_OPEN
                    return True
                return False
            # closed or half_open both permit an attempt.
            return True

    def record_success(self) -> None:
        """Reset failure tracking and close the circuit after a success."""
        with self._lock:
            self._consecutive_failures = 0
            self._state = _STATE_CLOSED

    def record_failure(self) -> None:
        """
        Register a failure, opening the circuit if the threshold is reached.
        """
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == _STATE_HALF_OPEN
                or self._consecutive_failures >= self.config.failure_threshold
            ):
                self._state = _STATE_OPEN
                self._opened_at_s = time.monotonic()


_registry: dict[str, CircuitBreaker] = {}
_registry_lock: threading.Lock = threading.Lock()


def get_circuit_breaker(name: str, config: CircuitBreakerConfig) -> CircuitBreaker:
    """
    Return a process-wide circuit breaker for ``name`` (created on first use).

    Args:
        name: Provider identifier.
        config: Tuning parameters applied when first created.

    Returns:
        CircuitBreaker: The shared breaker instance for the provider.
    """
    with _registry_lock:
        breaker = _registry.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name, config)
            _registry[name] = breaker
        return breaker


def load_qwen_circuit_breaker() -> CircuitBreaker:
    """
    Build the Qwen circuit breaker from environment configuration.

    Returns:
        CircuitBreaker: Breaker guarding the Qwen provider.
    """
    threshold: int = max(1, int(os.getenv("QWEN_CB_FAILURE_THRESHOLD", "3")))
    cooldown_s: float = max(1.0, float(os.getenv("QWEN_CB_COOLDOWN_S", "60")))
    return get_circuit_breaker(
        "qwen",
        CircuitBreakerConfig(failure_threshold=threshold, cooldown_s=cooldown_s),
    )


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "get_circuit_breaker",
    "load_qwen_circuit_breaker",
]

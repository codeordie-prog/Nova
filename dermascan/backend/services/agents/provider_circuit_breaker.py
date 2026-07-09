"""
Author: Dermascan Team
Date: 6-20-2026
Licence: MIT

Thread-safe circuit breaker for model providers.

Prevents runaway retries against an unhealthy provider. Failures are tracked in a
sliding time window (concurrency-correct, unlike a shared "consecutive" counter):
the circuit opens when, within the window, the failure count and failure ratio
both cross their thresholds and a minimum request volume has been observed. When
open, the circuit stays open for a jittered cooldown so many workers do not probe
in lockstep. After the cooldown a single half-open probe is allowed; success
closes the circuit, failure re-opens it.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Final, Tuple

_STATE_CLOSED: Final[str] = "closed"
_STATE_OPEN: Final[str] = "open"
_STATE_HALF_OPEN: Final[str] = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """
    Circuit breaker tuning parameters.

    Args:
        failure_threshold: Failures within ``window_s`` required to trip the circuit.
        window_s: Sliding window (seconds) over which failures/successes are counted.
        cooldown_s: Base seconds the circuit stays open before a half-open probe.
        cooldown_jitter_s: Max random seconds added to each open period so workers
            do not all probe at the same instant.
        min_volume: Minimum total requests observed in the window before the circuit
            may trip (prevents tripping on a tiny, unrepresentative sample).
        failure_ratio: Minimum failure fraction in the window required to trip
            (0.0-1.0); healthy parallel traffic dilutes rare failures.
    """

    failure_threshold: int = 3
    window_s: float = 30.0
    cooldown_s: float = 20.0
    cooldown_jitter_s: float = 5.0
    min_volume: int = 3
    failure_ratio: float = 0.5


class CircuitBreaker:
    """
    A thread-safe, sliding-window circuit breaker for a single provider.

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
        # (monotonic_ts, success) events pruned to the sliding window.
        self._events: Deque[Tuple[float, bool]] = deque()
        self._open_until_s: float = 0.0

    @property
    def state(self) -> str:
        """Return the current circuit state (closed/open/half_open)."""
        with self._lock:
            return self._state

    def _prune(self, now: float) -> None:
        """
        Drop events older than the sliding window.

        Args:
            now: Current monotonic timestamp.
        """
        cutoff: float = now - self.config.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _open(self, now: float) -> None:
        """
        Trip the circuit open with a jittered cooldown and clear the window.

        Args:
            now: Current monotonic timestamp.
        """
        jitter: float = random.uniform(0.0, max(0.0, self.config.cooldown_jitter_s))
        self._state = _STATE_OPEN
        self._open_until_s = now + self.config.cooldown_s + jitter
        self._events.clear()

    def allow(self) -> bool:
        """
        Decide whether a request to the provider should be attempted now.

        Returns:
            bool: True if the call may proceed; False if the circuit is open.
        """
        with self._lock:
            if self._state == _STATE_OPEN:
                if time.monotonic() >= self._open_until_s:
                    # Cooldown elapsed: allow a single half-open probe.
                    self._state = _STATE_HALF_OPEN
                    return True
                return False
            # closed or half_open both permit an attempt.
            return True

    def record_success(self) -> None:
        """Record a success; a half-open probe success closes and resets the circuit."""
        with self._lock:
            now: float = time.monotonic()
            if self._state == _STATE_HALF_OPEN:
                self._state = _STATE_CLOSED
                self._events.clear()
                return
            self._events.append((now, True))
            self._prune(now)

    def record_failure(self) -> None:
        """
        Register a failure, opening the circuit when window thresholds are crossed.

        A failure during a half-open probe immediately reopens the circuit.
        """
        with self._lock:
            now: float = time.monotonic()
            if self._state == _STATE_HALF_OPEN:
                self._open(now)
                return

            self._events.append((now, False))
            self._prune(now)

            total: int = len(self._events)
            failures: int = sum(1 for _, ok in self._events if not ok)
            if (
                total >= self.config.min_volume
                and failures >= self.config.failure_threshold
                and (failures / total) >= self.config.failure_ratio
            ):
                self._open(now)


_registry: dict[str, CircuitBreaker] = {}
_registry_lock: threading.Lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    """
    Read an integer environment variable, tolerating float strings.

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        int: Parsed value clamped to at least 1.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return max(1, default)
    return max(1, int(float(raw.strip())))


def _env_float(name: str, default: float) -> float:
    """
    Read a float environment variable.

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        float: Parsed value clamped to at least 1.0.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return max(1.0, default)
    return max(1.0, float(raw.strip()))


def _env_nonneg_float(name: str, default: float) -> float:
    """
    Read a non-negative float environment variable (allows 0.0).

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        float: Parsed value clamped to at least 0.0.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return max(0.0, default)
    return max(0.0, float(raw.strip()))


def _env_ratio(name: str, default: float) -> float:
    """
    Read a ratio environment variable clamped to the ``[0.0, 1.0]`` range.

    Args:
        name: Environment variable name.
        default: Value when unset or empty.

    Returns:
        float: Parsed value clamped to ``[0.0, 1.0]``.
    """
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw.strip())
    return min(1.0, max(0.0, value))


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


def load_provider_circuit_breaker(
    provider: str,
    *,
    threshold_env: str,
    cooldown_env: str,
    window_env: str = "MODEL_CB_WINDOW_S",
    jitter_env: str = "MODEL_CB_COOLDOWN_JITTER_S",
    min_volume_env: str = "MODEL_CB_MIN_VOLUME",
    failure_ratio_env: str = "MODEL_CB_FAILURE_RATIO",
    default_threshold: int = 3,
    default_cooldown_s: float = 20.0,
    default_window_s: float = 30.0,
    default_jitter_s: float = 5.0,
    default_min_volume: int = 3,
    default_failure_ratio: float = 0.5,
) -> CircuitBreaker:
    """
    Build a provider circuit breaker from environment configuration.

    Args:
        provider: Provider identifier used as the breaker registry key.
        threshold_env: Env var for windowed failure count to trip.
        cooldown_env: Env var for base open-state cooldown seconds.
        window_env: Env var for the sliding failure window in seconds.
        jitter_env: Env var for max random cooldown jitter seconds.
        min_volume_env: Env var for minimum window request volume before tripping.
        failure_ratio_env: Env var for minimum window failure ratio to trip.
        default_threshold: Default failure count when the env var is unset.
        default_cooldown_s: Default cooldown when the env var is unset.
        default_window_s: Default sliding window when the env var is unset.
        default_jitter_s: Default cooldown jitter when the env var is unset.
        default_min_volume: Default minimum volume when the env var is unset.
        default_failure_ratio: Default failure ratio when the env var is unset.

    Returns:
        CircuitBreaker: Breaker guarding the named provider.
    """
    config = CircuitBreakerConfig(
        failure_threshold=_env_int(threshold_env, default_threshold),
        window_s=_env_float(window_env, default_window_s),
        cooldown_s=_env_float(cooldown_env, default_cooldown_s),
        cooldown_jitter_s=_env_nonneg_float(jitter_env, default_jitter_s),
        min_volume=_env_int(min_volume_env, default_min_volume),
        failure_ratio=_env_ratio(failure_ratio_env, default_failure_ratio),
    )
    return get_circuit_breaker(provider, config)


def load_model_circuit_breaker(model_key: str) -> CircuitBreaker:
    """
    Build a per-model circuit breaker from environment configuration.

    Args:
        model_key: Registry key, e.g. ``openai:gpt-4o`` or ``gemini:gemini-3.5-flash``.

    Returns:
        CircuitBreaker: Breaker guarding the named model.
    """
    return load_provider_circuit_breaker(
        model_key,
        threshold_env="MODEL_CB_FAILURE_THRESHOLD",
        cooldown_env="MODEL_CB_COOLDOWN_S",
        default_threshold=_env_int("OPENAI_CB_FAILURE_THRESHOLD", 3),
        default_cooldown_s=_env_float("OPENAI_CB_COOLDOWN_S", 20.0),
    )


def load_openai_circuit_breaker() -> CircuitBreaker:
    """
    Build the OpenAI circuit breaker from environment configuration.

    Returns:
        CircuitBreaker: Breaker guarding the OpenAI provider.
    """
    return load_provider_circuit_breaker(
        "openai",
        threshold_env="OPENAI_CB_FAILURE_THRESHOLD",
        cooldown_env="OPENAI_CB_COOLDOWN_S",
    )


def load_gemini_circuit_breaker() -> CircuitBreaker:
    """
    Build the Gemini circuit breaker from environment configuration.

    Returns:
        CircuitBreaker: Breaker guarding the Gemini provider.
    """
    return load_provider_circuit_breaker(
        "gemini",
        threshold_env="GEMINI_CB_FAILURE_THRESHOLD",
        cooldown_env="GEMINI_CB_COOLDOWN_S",
    )


def load_qwen_circuit_breaker() -> CircuitBreaker:
    """
    Build the Qwen circuit breaker from environment configuration.

    Returns:
        CircuitBreaker: Breaker guarding the Qwen provider.
    """
    return load_provider_circuit_breaker(
        "qwen",
        threshold_env="QWEN_CB_FAILURE_THRESHOLD",
        cooldown_env="QWEN_CB_COOLDOWN_S",
    )


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "get_circuit_breaker",
    "load_provider_circuit_breaker",
    "load_model_circuit_breaker",
    "load_openai_circuit_breaker",
    "load_gemini_circuit_breaker",
    "load_qwen_circuit_breaker",
]

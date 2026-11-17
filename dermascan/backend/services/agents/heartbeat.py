"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-09-24
Licence: PolyForm Noncommercial License 1.0.0

Stream message liveness heartbeat.

Keeps a long-running, in-flight Redis Stream message from being reclaimed by
resetting its pending idle time via ``XCLAIM`` while the owning consumer is
still processing it. This lets the reclaim loop distinguish genuinely dead
workers from slow-but-healthy turns, eliminating churn where reclaim races a
live turn that merely outlived the idle threshold.


# ENSURE YOU UNDERSTAND RELATIONSHIP BETWEEN LOCK, LOCK EXTENDER, HEARTBEAT, IN-FLIGHT MARKER, AND RECLAIM.
"""


from __future__ import annotations

import threading
from typing import Optional

import redis

from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

# Absolute floor for the heartbeat cadence; guards against pathologically small
# idle thresholds producing a hot loop against Redis.
_MIN_INTERVAL_SEC: float = 5.0

# Fraction of the smallest relevant deadline used as the heartbeat period. A
# third gives at least two refreshes before the idle threshold is reached even
# if one beat is delayed.
_DEADLINE_FRACTION: float = 3.0


def compute_heartbeat_interval_sec(
    pending_min_idle_ms: int,
    lease_sec: Optional[int] = None,
) -> float:
    """
    Compute a heartbeat interval safely below the reclaim idle threshold.

    The interval must be small enough that a message's idle time cannot reach
    ``pending_min_idle_ms`` between two consecutive heartbeats. When a client
    lock lease is supplied, the interval also stays under the lease so lock and
    stream liveness refresh on comparable cadences.

    Args:
        pending_min_idle_ms: Reclaim idle threshold in milliseconds.
        lease_sec: Optional client-lock lease seconds to also stay under.

    Returns:
        Heartbeat interval in seconds, floored at ``_MIN_INTERVAL_SEC``.

    Time complexity: O(1). Space complexity: O(1).
    """
    idle_sec = max(_MIN_INTERVAL_SEC * _DEADLINE_FRACTION, pending_min_idle_ms / 1000.0)
    candidate = idle_sec / _DEADLINE_FRACTION
    if lease_sec is not None and lease_sec > 0:
        candidate = min(candidate, lease_sec / _DEADLINE_FRACTION)
    return max(_MIN_INTERVAL_SEC, candidate)


def start_stream_heartbeat(
    redis_client: redis.Redis,
    *,
    stream: str,
    group: str,
    consumer: str,
    msg_id: str,
    interval_sec: float,
) -> threading.Event:
    """
    Start a daemon heartbeat that resets a pending message's idle time.

    While active, the thread periodically issues ``XCLAIM ... JUSTID`` with a
    zero ``min-idle`` so the message is re-asserted to the owning consumer and
    its idle timer resets. Reclaim (``XAUTOCLAIM`` with a non-zero min-idle)
    therefore skips this message until heartbeats stop.

    Crash-safety is preserved: if the worker dies, heartbeats cease and the
    idle time grows until the normal reclaim threshold triggers recovery.

    Args:
        redis_client: Redis client used to issue ``XCLAIM``.
        stream: Stream key holding the pending message.
        group: Consumer group name.
        consumer: Owning consumer name the message is re-asserted to.
        msg_id: Pending stream message id to keep alive.
        interval_sec: Seconds between heartbeats; see
            :func:`compute_heartbeat_interval_sec`.

    Returns:
        threading.Event: Stop signal. Set it (in a ``finally`` block, before
        acking/deleting the message) to end the heartbeat.

    Time complexity: O(1) per beat. Space complexity: O(1).
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(interval_sec):
            try:
                redis_client.xclaim(
                    stream,
                    group,
                    consumer,
                    min_idle_time=0,
                    message_ids=[msg_id],
                    justid=True,
                )
            except Exception:
                # Best-effort liveness: a transient Redis error must never abort
                # the in-flight turn. The next beat retries; if Redis stays down
                # the message simply becomes reclaimable, which is the safe state.
                logger.warning(
                    "stream heartbeat failed: stream=%s group=%s msg_id=%s",
                    stream,
                    group,
                    msg_id,
                )

    threading.Thread(
        target=_beat,
        name=f"stream-heartbeat-{stream}-{msg_id}",
        daemon=True,
    ).start()
    return stop

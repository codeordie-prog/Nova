"""
Author: Dermascan Team
Date: 2026-08-21
License: MIT

Request-scoped billing context for metering hooks.

Set by the orchestrator worker at turn start; read by model chain hooks.

The billing scope tells the metering hooks which account and request id this turn belongs to.

Since middleware doesn't have access to the request id, we use ContextVar as the bridge. Otherwise, I would have to pass the request id in every layer.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class BillingScope:
    """
    Active billable scope for the current orchestrator turn.

    Attributes:
        account_id: Wallet uuid.
        scope_key: Correlation key (``request_id``).
        channel: Ingress channel.
        client_id: Messaging client id.
        reservation_id: Hold uuid when enforcement is on.
    """

    account_id: str
    scope_key: str
    channel: str
    client_id: str
    reservation_id: str | None = None
    _next_line_seq: int = field(default=1, repr=False)

    def next_line_seq(self) -> int:
        """
        Return the next usage-line sequence number for this scope.

        Returns:
            int: Monotonic line sequence (1-based).
        """
        seq = self._next_line_seq
        self._next_line_seq += 1
        return seq


billing_scope: ContextVar[BillingScope | None] = ContextVar(
    "billing_scope",
    default=None,
)

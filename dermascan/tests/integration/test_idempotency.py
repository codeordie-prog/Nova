"""Integration tests: duplicate message idempotency.

Concerns: R1 — exactly-once-ish processing; R10 — idempotent webhook re-delivery;
          C7 — idempotency under burst.
Requires real Redis (testcontainers).
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestIdempotency:
    """Duplicate msg_id processed once across reclaim and burst."""

    def test_duplicate_msg_id_single_processing(self, real_redis) -> None:
        """R10: Same msg_id enqueued twice → handler runs once."""
        pytest.skip("Scaffold: implementation pending")

    def test_mark_processed_set_nx_wins_once(self, real_redis) -> None:
        """C7: Parallel reclaim of same msg → only one SET NX wins."""
        pytest.skip("Scaffold: implementation pending")

    def test_second_pass_acks_without_reprocessing(self, real_redis) -> None:
        """R1: Second reclaim pass acks+deletes without re-running handler."""
        pytest.skip("Scaffold: implementation pending")

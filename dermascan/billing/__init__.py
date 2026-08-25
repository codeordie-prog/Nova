"""
Author: Dermascan Team
Date: 2026-08-25
License: MIT

Billing: credit accounts, the append-only ledger, metering, and Paystack top-ups.

Postgres is authoritative for money. Every balance change is a ledger row, and
``account.balance_credits`` is a cached sum that reconciliation asserts against
``SUM(ledger.delta_credits)``.

Two paths spend credits and one path adds them:

* **Metering** (``config``, ``context``, ``meter``, ``usage``) records what a
  turn actually cost in token terms, as balance-neutral ``usage_line`` rows.
* **Reservations** (``reservations``, ``medgemma``) hold a ceiling before the
  work runs and settle against those usage lines when it finishes. This is the
  live gate.
* **Top-ups** (``paystack_client``, ``intents``, ``topups``, ``fulfilment``,
  ``webhook``, ``notify``) turn a KES payment into credits.

``redis_wallet`` mirrors the balance into Redis for a future deduction hot path
and is kept coherent by write-through from the reservation calls. Its
``deduct``/``settle`` entry points are not on the live path today.

**The credit unit is KES.** One credit is one shilling — what a user tops up,
sees, and spends. Provider token prices are quoted in USD per million tokens
and are converted through the ``usd_kes_rate`` setting in ``billing.price_list``
when the rate card loads. Prices and rates are data, not environment: they live
in ``price_list`` and are edited from the admin portal.
"""

from dermascan.billing.accounts import (
    Account,
    get_balance,
    get_or_create_account,
    resolve_for_client,
)
from dermascan.billing.config import (
    BILLING_RATE_CARD_VERSION,
    LedgerKind,
    ReservationState,
    resolve_medgemma_job_type,
)
from dermascan.billing.context import BillingScope, billing_scope
from dermascan.billing.db import ping as billing_db_ping
from dermascan.billing.flags import (
    billing_active,
    billing_enabled,
    billing_enforce_enabled,
    billing_meter_enabled,
    validate_billing_flags,
)
from dermascan.billing.medgemma import (
    MedgemmaBillingContext,
    billing_context_from_mapping,
    billing_fields_for_stream,
    prepare_medgemma_job_billing,
    settle_medgemma_job,
)
from dermascan.billing.meter import record_llm_usage
from dermascan.billing.reservations import (
    InsufficientCreditsError,
    Reservation,
    commit_reservation,
    expire_stale_holds,
    get_reservation_id_by_key,
    place_hold,
    release_reservation,
)
from dermascan.billing.usage import record_inference_usage_line, record_usage_line

__all__ = [
    "Account",
    "BILLING_RATE_CARD_VERSION",
    "BillingScope",
    "InsufficientCreditsError",
    "LedgerKind",
    "MedgemmaBillingContext",
    "Reservation",
    "ReservationState",
    "billing_active",
    "billing_context_from_mapping",
    "billing_db_ping",
    "billing_enabled",
    "billing_enforce_enabled",
    "billing_fields_for_stream",
    "billing_meter_enabled",
    "billing_scope",
    "commit_reservation",
    "expire_stale_holds",
    "get_balance",
    "get_or_create_account",
    "get_reservation_id_by_key",
    "place_hold",
    "prepare_medgemma_job_billing",
    "record_inference_usage_line",
    "record_llm_usage",
    "record_usage_line",
    "release_reservation",
    "resolve_for_client",
    "resolve_medgemma_job_type",
    "settle_medgemma_job",
    "validate_billing_flags",
]

"""
Author: Dermascan Team
Date: 2026-08-21
License: MIT

Billing package — meter/hold lane (hold → meter → settle).

Paystack top-up and Redis wallet live on ``feat/billing-paystack-credits`` in the
same package (``topups``, ``paystack_client``, ``redis_wallet``, …).

Meter/hold modules (this branch):
  config, db, flags, context, accounts, ledger, reservations, usage, meter

Schema: ``cloud_sql/billing/SCHEMA_ALIGNMENT.md``
"""

from dermascan.billing.accounts import Account, get_balance, get_or_create_account
from dermascan.billing.config import (
    BILLING_ORCHESTRATOR_HOLD_CREDITS,
    BILLING_RATE_CARD_VERSION,
    LedgerKind,
    ReservationState,
)
from dermascan.billing.context import BillingScope, billing_scope
from dermascan.billing.db import ping as billing_db_ping
from dermascan.billing.flags import (
    billing_active,
    billing_enforce_enabled,
    billing_meter_enabled,
    validate_billing_flags,
)
from dermascan.billing.meter import record_llm_usage
from dermascan.billing.reservations import (
    INSUFFICIENT_CREDITS_MESSAGE,
    InsufficientCreditsError,
    Reservation,
    commit_reservation,
    expire_stale_holds,
    get_reservation_id_by_key,
    place_hold,
    release_reservation,
)
from dermascan.billing.usage import record_inference_usage_line, record_usage_line
from dermascan.billing.medgemma import (
    MedgemmaBillingContext,
    billing_context_from_mapping,
    billing_fields_for_stream,
    prepare_medgemma_job_billing,
    settle_medgemma_job,
)
from dermascan.billing.config import resolve_medgemma_job_type

__all__ = [
    "Account",
    "BillingScope",
    "INSUFFICIENT_CREDITS_MESSAGE",
    "InsufficientCreditsError",
    "Reservation",
    "BILLING_ORCHESTRATOR_HOLD_CREDITS",
    "BILLING_RATE_CARD_VERSION",
    "LedgerKind",
    "ReservationState",
    "billing_active",
    "billing_db_ping",
    "billing_enforce_enabled",
    "billing_meter_enabled",
    "billing_scope",
    "commit_reservation",
    "expire_stale_holds",
    "get_reservation_id_by_key",
    "get_balance",
    "get_or_create_account",
    "place_hold",
    "record_inference_usage_line",
    "record_llm_usage",
    "record_usage_line",
    "release_reservation",
    "resolve_medgemma_job_type",
    "settle_medgemma_job",
    "validate_billing_flags",
]

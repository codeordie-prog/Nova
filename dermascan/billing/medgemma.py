"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-21
License: MIT

MedGemma flat-SKU billing (hold at enqueue, meter + settle on worker completion).

Reservation key: ``medgemma_job_id`` (the Redis stream job id).
"""


from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from dermascan.billing.accounts import get_or_create_account
from dermascan.billing.config import (
    medgemma_hold_credits,
    BillingJobType,
    credits_for_medgemma_job,
    resolve_medgemma_job_type,
)
from dermascan.billing.flags import billing_active, billing_enforce_enabled
from dermascan.billing.reservations import (
    InsufficientCreditsError,
    commit_reservation,
    get_reservation_id_by_key,
    place_hold,
)
from dermascan.billing.usage import record_inference_usage_line
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class MedgemmaBillingContext:
    """Billing metadata persisted for one MedGemma job."""

    account_id: str
    job_type: str
    reservation_id: Optional[str] = None


def prepare_medgemma_job_billing(
    *,
    job_id: str,
    channel: str,
    client_id: str,
    prompt: str,
    case_id: str = "",
    explicit_job_type: Optional[str] = None,
) -> Optional[MedgemmaBillingContext]:
    """
    Create account + optional hold before a MedGemma job is enqueued.

    Args:
        job_id: MedGemma job uuid (reservation key).
        channel: Ingress channel.
        client_id: Messaging client id.
        prompt: Prompt/context used to infer job type when not explicit.
        case_id: Optional clinical case id.
        explicit_job_type: Optional :class:`BillingJobType` override.

    Returns:
        Optional[MedgemmaBillingContext]: Context when billing is active and
        ``(channel, client_id)`` are present; otherwise ``None``.

    Raises:
        InsufficientCreditsError: When enforce is on and balance is too low.
    """
    if not billing_active():
        return None
    if not channel.strip() or not client_id.strip():
        logger.info(
            "medgemma billing skipped at enqueue: missing identity job_id=%s",
            job_id,
        )
        return None

    try:
        account = get_or_create_account(channel.strip(), client_id.strip())

    except Exception:
        logger.exception(
            "medgemma billing account lookup failed: job_id=%s channel=%s client_id=%s",
            job_id,
            channel,
            client_id,
        )
        if billing_enforce_enabled():
            raise
        return None

    job_type = resolve_medgemma_job_type(prompt, explicit=explicit_job_type)
    reservation_id: Optional[str] = None

    try:
        if billing_enforce_enabled():

            # Reserve credits for the MedGemma job
            reservation = place_hold(
                account_id=account.id,
                reservation_key=job_id,
                job_type=job_type,
                hold_credits=medgemma_hold_credits(),
                case_id=case_id or None,
                client_id=client_id,
                channel=channel,
            )
            reservation_id = reservation.id
            
    except InsufficientCreditsError:
        raise
    except Exception:
        logger.exception(
            "medgemma billing hold failed: job_id=%s account_id=%s",
            job_id,
            account.id,
        )
        if billing_enforce_enabled():
            raise
        reservation_id = None

    logger.info(
        "medgemma billing prepared: job_id=%s job_type=%s reservation_id=%s",
        job_id,
        job_type,
        reservation_id,
    )
    return MedgemmaBillingContext(
        account_id=account.id,
        job_type=job_type,
        reservation_id=reservation_id,
    )


def billing_fields_for_stream(
    context: Optional[MedgemmaBillingContext],
) -> dict[str, str]:
    """
    Serialize billing metadata for Redis stream/job hash fields.

    Args:
        context: Prepared billing context, if any.

    Returns:
        dict[str, str]: Stream fields to merge into ``xadd`` / ``hset``.
    """
    if context is None:
        return {}
    fields = {
        "billing_job_type": context.job_type,
        "billing_account_id": context.account_id,
    }
    if context.reservation_id:
        fields["billing_reservation_id"] = context.reservation_id
    return fields


def billing_context_from_mapping(
    fields: Mapping[str, Any],
) -> Optional[MedgemmaBillingContext]:
    """
    Rehydrate billing context from stream payload or Redis job hash fields.

    Args:
        fields: Mapping of job metadata.

    Returns:
        Optional[MedgemmaBillingContext]: Parsed context when account id exists.
    """
    account_id = _field_str(fields, "billing_account_id")
    if not account_id:
        return None
    job_type = _field_str(fields, "billing_job_type") or BillingJobType.SKIN_AUDIT.value
    reservation_id = _field_str(fields, "billing_reservation_id") or None
    return MedgemmaBillingContext(
        account_id=account_id,
        job_type=job_type,
        reservation_id=reservation_id,
    )


def settle_medgemma_job(
    *,
    job_id: str,
    success: bool,
    billing_context: Optional[MedgemmaBillingContext] = None,
) -> None:
    """
    Meter a successful inference job and settle any held reservation.

    Args:
        job_id: MedGemma job uuid.
        success: Whether inference completed successfully.
        billing_context: Optional preloaded billing metadata.
    """
    if not billing_active():
        return

    context = billing_context
    reservation_id: Optional[str] = None
    if context is not None:
        reservation_id = context.reservation_id

    reservation_id = reservation_id or get_reservation_id_by_key(job_id)

    try:
        if success and context is not None and context.account_id:
            cost = credits_for_medgemma_job(context.job_type)
            if cost > 0:
                record_inference_usage_line(
                    account_id=context.account_id,
                    scope_key=job_id,
                    job_type=context.job_type,
                    cost_credits=cost,
                    reservation_id=reservation_id,
                )
    except Exception:
        logger.exception(
            "medgemma billing meter failed: job_id=%s success=%s",
            job_id,
            success,
        )

    try:
        if billing_enforce_enabled() and reservation_id:
            commit_reservation(reservation_id)
    except Exception:
        logger.exception(
            "medgemma billing settle failed: job_id=%s reservation_id=%s",
            job_id,
            reservation_id,
        )

    logger.info(
        "medgemma billing settled: job_id=%s success=%s job_type=%s reservation_id=%s",
        job_id,
        success,
        context.job_type if context is not None else "unknown",
        reservation_id,
    )


def _field_str(fields: Mapping[str, Any], key: str) -> str:
    """Return a string field from bytes or str keyed stream/job data."""
    value = fields.get(key)
    if value is None:
        value = fields.get(key.encode("utf-8"))  # type: ignore[index]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore").strip()
    return "" if value is None else str(value).strip()

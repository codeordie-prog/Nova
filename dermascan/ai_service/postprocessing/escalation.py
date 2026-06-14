"""
Author: Dermascan Team
Date: 2026-4-29
Licence: MIT

Description: This module contains the implementation of the Escalation logic post-processing

The idea relies on the confidence score threshold bar set for the diagnosis model.

If the calculated confidence score falls below the threshold, the case is ESCALATED
to a certified Physician for further assesment.

In-addition to this, there are other routing logic that involve passing certain cases to
the Physician irregardless of the confidence score - This is purposefully to improve
the quality of the product as time goes by.

"""
import os
import json

from dotenv import load_dotenv
from typing import Dict, Any
from dataclasses import dataclass

from redis import Redis

# load environment variables
load_dotenv()

# Get environment variables
_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_ORCHESTRATOR_SIGNAL_STREAM: str = os.environ.get("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
_EVENTS_STREAM: str = os.environ.get("EVENTS_STREAM","events:stream")
_ESCALATION_STREAM: str = os.environ.get("ESCALATION_STREAM", "escalation:jobs")
_EVENTS_STREAM_MAXLEN: int = int(os.environ.get("WORKERS_STREAM_MAXLEN", "5000"))
_DIAGNOSE_CONFIDENCE_THRESHOLD: float = float(os.environ.get("DIAGNOSE_CONFIDENCE_THRESHOLD", "0.75"))
_PRODUCT_REVIEW_CONFIDENCE_THRESHOLD: float = float(os.environ.get("PRODUCT_REVIEW_CONFIDENCE_THRESHOLD", "0.60"))
_ESCALATION_DLQ_STREAM: str = os.environ.get("ESCALATION_DLQ_STREAM", "escalation:dlq")
_EVENTS_DLQ_STREAM: str = os.environ.get("EVENTS_DLQ_STREAM","events:dlq")


# Initialize REDIS client
_redis: Redis = Redis.from_url(_REDIS_URL)

@dataclass
class _JobEntry:
    """
    One Job collected during batch window
    """
    job_id: str
    prompt: str
    image_bytes: bytes
    msg_id: str
    request_id: str
    case_id: str
    client_id: str
    message_id: str
    channel: str
    group_id: str = ""
    group_expected_count: int = 0


def _to_str(value: Any) -> str:
    """
    Convert a value to a string, handling bytes and None.
    
    Args:
        value: The value to convert
        
    Returns:
        The string representation of the value
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return "" if value is None else str(value)


def _extract_structured_fields(text: str) -> tuple[dict[str, Any], bool]:
    """
    Parse MedGemma JSON output and extract diagnose/product fields.

    Args:
        text: The JSON text to parse
        
    Returns:
        A tuple of (fields dictionary, parse_success)
    """
    fields: dict[str, Any] = {
        "review_type": "",
        "diagnosis": "",
        "rationale": "",
        "summary": {
            "severity": "",
            "active_inflammation": "",
            "barrier_status": "",
            "observed_patterns": [],
        },
        "treatment": {
            "needs": {
                "barrier_repair": "",
                "anti_inflammatory": "",
                "comedolytic": "",
                "hydration": "",
                "sun_protection": "",
            },
            "contraindicated_approaches": [],
        },
        "product_name": "",
        "description": "",
        "indications": "",
        "safety_in_pregnancy": "",
        "contraindications": "",
        "how_to_use": "",
        "special_instructions": "",
    }
    try:
        parsed: dict[str, Any] = json.loads(text)
    except Exception:
        return fields, False

    for key in (
        "review_type",
        "diagnosis",
        "rationale",
        "product_name",
        "description",
        "indications",
        "safety_in_pregnancy",
        "contraindications",
        "how_to_use",
        "special_instructions",
    ):
        fields[key] = _to_str(parsed.get(key)).strip()

    parsed_summary = parsed.get("summary")
    if isinstance(parsed_summary, dict):
        fields["summary"] = {
            "severity": _to_str(parsed_summary.get("severity")).strip(),
            "active_inflammation": _to_str(parsed_summary.get("active_inflammation")).strip(),
            "barrier_status": _to_str(parsed_summary.get("barrier_status")).strip(),
            "observed_patterns": [
                _to_str(item).strip()
                for item in (parsed_summary.get("observed_patterns") or [])
                if _to_str(item).strip()
            ]
            if isinstance(parsed_summary.get("observed_patterns"), list)
            else [],
        }

    parsed_treatment = parsed.get("treatment")
    if isinstance(parsed_treatment, dict):
        parsed_needs = parsed_treatment.get("needs")
        needs_obj = {
            "barrier_repair": "",
            "anti_inflammatory": "",
            "comedolytic": "",
            "hydration": "",
            "sun_protection": "",
        }
        if isinstance(parsed_needs, dict):
            needs_obj = {
                "barrier_repair": _to_str(parsed_needs.get("barrier_repair")).strip(),
                "anti_inflammatory": _to_str(parsed_needs.get("anti_inflammatory")).strip(),
                "comedolytic": _to_str(parsed_needs.get("comedolytic")).strip(),
                "hydration": _to_str(parsed_needs.get("hydration")).strip(),
                "sun_protection": _to_str(parsed_needs.get("sun_protection")).strip(),
            }
        fields["treatment"] = {
            "needs": needs_obj,
            "contraindicated_approaches": [
                _to_str(item).strip()
                for item in (parsed_treatment.get("contraindicated_approaches") or [])
                if _to_str(item).strip()
            ]
            if isinstance(parsed_treatment.get("contraindicated_approaches"), list)
            else [],
        }
    return fields, True


def route_case_after_inference(entry: _JobEntry, text: str, confidence: float | None) -> bool:
    """
    Determines whether a case should be escalated to a human expert or NOT.

    Args:
        entry: The job entry containing case information
        text: The JSON text to parse
        confidence: The confidence score of the diagnosis
        
    Returns:
        True if the case should be escalated, False otherwise
    """
    structured, parsed_ok = _extract_structured_fields(text)
    product_fields = (
        "product_name",
        "description",
        "indications",
        "safety_in_pregnancy",
        "contraindications",
        "how_to_use",
        "special_instructions",
    )

    review_type = structured.get("review_type", "").strip().lower()
    diagnosis_present = bool(structured["diagnosis"])
    product_present = any(bool(structured.get(k)) for k in product_fields)

    if review_type in ("product review", "product_review", "product", "hybrid review", "hybrid_review", "hybrid"):
        mode = "product_review"
    elif review_type in ("skin review", "skin_review", "skin", "general review", "general_review", "general"):
        mode = "diagnose"
    elif product_present and diagnosis_present:
        mode = "hybrid"
    elif product_present:
        mode = "product_review"
    elif diagnosis_present:
        mode = "diagnose"
    else:
        # Safe fallback for malformed or empty payloads.
        mode = "diagnose"

    threshold_used = (
        _PRODUCT_REVIEW_CONFIDENCE_THRESHOLD if mode == "product_review" else _DIAGNOSE_CONFIDENCE_THRESHOLD
    )
    escalate: bool = confidence is None or confidence < threshold_used

    if not parsed_ok:
        escalate = True

    completed_status = f"{mode}_inference_completed"
    escalated_status = f"{mode}_escalated"
    escalation_note = (
        "Model response was not valid JSON"
        if not parsed_ok
        else ("Product review confidence fell below threshold" if mode == "product_review" and escalate
              else ("Diagnosis confidence fell below threshold" if escalate else ""))
    )

    base_fields = {
        "review_type": structured["review_type"],
        "rationale": structured["rationale"],
        "product_name": structured["product_name"],
        "description": structured["description"],
        "indications": structured["indications"],
        "safety_in_pregnancy": structured["safety_in_pregnancy"],
        "contraindications": structured["contraindications"],
        "how_to_use": structured["how_to_use"],
        "special_instructions": structured["special_instructions"],
        "diagnosis": structured["diagnosis"],
        "summary": structured["summary"] if isinstance(structured["summary"], dict) else _to_str(structured["summary"]).strip(),
        "treatment": structured["treatment"] if isinstance(structured["treatment"], dict) else _to_str(structured["treatment"]).strip(),
    }

    # push case to EVENT stream - regardless of confidence threshold
    _push_event(
        {
            "request_id": entry.request_id,
            "case_id": entry.case_id,
            "client_id": entry.client_id,
            "message_id": entry.message_id,
            "channel": entry.channel,
            "agent": "medgemma",
            "mode": mode,
            "status": completed_status,
            "confidence": "" if confidence is None else str(confidence),
            "threshold": str(threshold_used),
            "escalation": str(escalate).lower(),
            "escalation_note": escalation_note,
            **base_fields,
        }
    )

    if escalate:

        _push_escalation(entry, text, confidence, base_fields, threshold_used, mode)
        _push_event(
            {
                "request_id": entry.request_id,
                "case_id": entry.case_id,
                "client_id": entry.client_id,
                "message_id": entry.message_id,
                "channel": entry.channel,
                "agent": "medgemma",
                "mode": mode,
                "status": escalated_status,
                "confidence": "" if confidence is None else str(confidence),
                "threshold": str(threshold_used),
                "escalation": "true",
                **base_fields,
            }
        )

    _push_signal(
        entry=entry,
        text=text,
        confidence=confidence,
        threshold_used=threshold_used,
        mode=mode,
        escalated=escalate,
        base_fields=base_fields,
        parsed_ok=parsed_ok,
    )

    return escalate


def _route_case_after_inference(entry: _JobEntry, text: str, confidence: float | None) -> bool:
    return route_case_after_inference(entry, text, confidence)


def _push_escalation(
    entry: _JobEntry,
    text: str,
    confidence: float | None,
    base_fields: dict[str, str],
    threshold_used: float,
    mode: str,
) -> None:
    """
    Push a case that falls below the confidence threshold to ESCALATION STREAM.

    Args:
        entry: The job entry
        text: The diagnosis text
        confidence: The confidence score
    """
    _redis.xadd(
        _ESCALATION_STREAM,
        {
            "request_id": entry.request_id,
            "case_id": entry.case_id,
            "client_id": entry.client_id,
            "message_id": entry.message_id,
            "channel": entry.channel,
            "source_agent": "medgemma",
            "mode": mode,
            "reason": "low_confidence",
            "confidence": "" if confidence is None else str(confidence),
            "threshold": str(threshold_used),
            "diagnosis_text": base_fields.get("diagnosis") or text,
            "requires_human_review": "true",
            "product_name": base_fields.get("product_name", ""),
            "description": base_fields.get("description", ""),
            "indications": base_fields.get("indications", ""),
            "safety_in_pregnancy": base_fields.get("safety_in_pregnancy", ""),
            "contraindications": base_fields.get("contraindications", ""),
            "how_to_use": base_fields.get("how_to_use", ""),
            "special_instructions": base_fields.get("special_instructions", ""),
        },
        maxlen=_EVENTS_STREAM_MAXLEN,
        approximate=True,

    )

def _push_signal(
    entry: _JobEntry,
    text: str,
    confidence: float | None,
    threshold_used: float,
    mode: str,
    escalated: bool,
    base_fields: dict[str, str],
    parsed_ok: bool,
) -> None:
    """
    Push a completion/escalation signal to ORCHESTRATOR SIGNAL STREAM.

    Args:
        entry: The job entry
        text: The model response text
        confidence: The confidence score
    """
    signal_type = f"{mode}_escalated" if escalated else f"{mode}_completed"
    if not parsed_ok:
        notes = "The model response was not valid JSON"
    elif escalated:
        notes = f"The {mode} inference completed below confidence threshold"
    else:
        notes = f"The {mode} inference completed above threshold"

    _redis.xadd(
        _ORCHESTRATOR_SIGNAL_STREAM,
        {
            "signal_type": signal_type,
            "signal_id": entry.msg_id,
            "request_id": entry.request_id,
            "case_id": entry.case_id,
            "client_id": entry.client_id,
            "message_id": entry.message_id,
            "channel": entry.channel,
            "source_event_id": "",
            "payload": json.dumps(
                {
                    "review_type": base_fields.get("review_type", ""),
                    "diagnosis": base_fields.get("diagnosis") or "",
                    "diagnosis_text": base_fields.get("diagnosis") or text,
                    "rationale": base_fields.get("rationale", ""),
                    "summary": base_fields.get("summary", {}),
                    "treatment": base_fields.get("treatment", {}),
                    "confidence": confidence,
                    "threshold": threshold_used,
                    "escalation": escalated,
                    "mode": mode,
                    "product_name": base_fields.get("product_name", ""),
                    "description": base_fields.get("description", ""),
                    "indications": base_fields.get("indications", ""),
                    "safety_in_pregnancy": base_fields.get("safety_in_pregnancy", ""),
                    "contraindications": base_fields.get("contraindications", ""),
                    "how_to_use": base_fields.get("how_to_use", ""),
                    "special_instructions": base_fields.get("special_instructions", ""),
                    "metadata": {
                        "extras": {
                            "group_id": entry.group_id,
                            "group_expected_count": str(entry.group_expected_count),
                        }
                    },
                }
            ),
            "notes": notes,
        },
        maxlen=_EVENTS_STREAM_MAXLEN,
        approximate=True,

    )


def _push_event(fields: Dict[str, Any]) -> None:
    """
    Push a case to EVENT STREAM.

    Args:
        fields: The fields to emit
    """
    
    _redis.xadd(
        _EVENTS_STREAM,
        {
            k: _to_str(v) for k, v in fields.items()
        },
        maxlen=_EVENTS_STREAM_MAXLEN,
        approximate=True
    )
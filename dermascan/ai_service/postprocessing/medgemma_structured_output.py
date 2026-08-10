"""
Author: Dermascan Team
Date: 2026-08-07
License: MIT

Parse and normalize MedGemma structured JSON outputs.

Supports legacy clinical fields and additive cosmetic-intelligence fields during
the pivot transition (dual-write schema).
"""

from __future__ import annotations

import json
from typing import Any, Literal

# Define the risk tiers and image qualities
RiskTier = Literal["cosmetic", "watch", "urgent"]
ImageQuality = Literal["good", "poor", "unusable"]

# Define the cosmetic schema version and valid risk tiers and image qualities
COSMETIC_SCHEMA_VERSION: str = "cosmetic_v1"
_VALID_RISK_TIERS: frozenset[str] = frozenset({"cosmetic", "watch", "urgent"})
_VALID_IMAGE_QUALITIES: frozenset[str] = frozenset({"good", "poor", "unusable"})


def _to_str(value: Any) -> str:
    """
    Coerce a value to string for safe field extraction.

    Args:
        value: Value to coerce to string.

    Returns:
        str: Coerced value as string.
    """
    if value is None:
        return ""
    return str(value)


def _empty_summary() -> dict[str, Any]:
    """
    Return an empty summary dictionary.

    Returns:
        dict[str, Any]: Empty summary dictionary.
    """
    return {
        "severity": "",
        "active_inflammation": "",
        "barrier_status": "",
        "observed_patterns": [],
    }


def _empty_needs() -> dict[str, str]:
    """
    Return an empty needs dictionary.

    Returns:
        dict[str, str]: Empty needs dictionary.
    """
    return {
        "barrier_repair": "",
        "anti_inflammatory": "",
        "comedolytic": "",
        "hydration": "",
        "sun_protection": "",
    }


def _empty_implications() -> dict[str, Any]:
    """
    Return an empty implications dictionary.

    Returns:
        dict[str, Any]: Empty implications dictionary.
    """
    return {
        "needs": _empty_needs(),
        "contraindicated_approaches": [],
        "avoid_for_now": [],
    }


def default_structured_fields() -> dict[str, Any]:
    """
    Build the default empty structured-output field mapping.

    Returns:
        dict[str, Any]: Empty template for MedGemma JSON parsing.
    """
    return {
        "schema_version": "",
        "review_type": "",
        "diagnosis": "",
        "primary_observation": "",
        "rationale": "",
        "observation_rationale": "",
        "summary": _empty_summary(),
        "skin_state": _empty_summary(),
        "treatment": {
            "needs": _empty_needs(),
            "contraindicated_approaches": [],
        },
        "cosmetic_implications": _empty_implications(),
        "risk_tier": "cosmetic",
        "image_quality": "unknown",
        "product_name": "",
        "description": "",
        "indications": "",
        "safety_in_pregnancy": "",
        "contraindications": "",
        "how_to_use": "",
        "special_instructions": "",
    }


def normalize_risk_tier(value: str | None) -> RiskTier:
    """
    Coerce a risk tier string to a valid enum value.

    Args:
        value: Raw model output.

    Returns:
        RiskTier: Normalized tier, defaulting to ``cosmetic``.
    """
    normalized = str(value or "").strip().lower()
    if normalized in _VALID_RISK_TIERS:
        return normalized  # type: ignore[return-value]
    return "cosmetic"


def normalize_image_quality(value: str | None) -> str:
    """
    Coerce image quality to a known label.

    Args:
        value: Raw model output.

    Returns:
        str: ``good``, ``poor``, ``unusable``, or ``unknown``.
    """
    normalized = str(value or "").strip().lower()
    if normalized in _VALID_IMAGE_QUALITIES:
        return normalized
    return "unknown"


def _parse_summary_block(raw: Any) -> dict[str, Any]:
    """
    Parse the summary block from the raw JSON.

    Args:
        raw: Raw JSON object.

    Returns:
        dict[str, Any]: Parsed summary dictionary.
    """
    if not isinstance(raw, dict):
        return _empty_summary()
    return {
        "severity": _to_str(raw.get("severity")).strip(),
        "active_inflammation": _to_str(raw.get("active_inflammation")).strip(),
        "barrier_status": _to_str(raw.get("barrier_status")).strip(),
        "observed_patterns": [
            _to_str(item).strip()
            for item in (raw.get("observed_patterns") or [])
            if _to_str(item).strip()
        ]
        if isinstance(raw.get("observed_patterns"), list)
        else [],
    }


def _parse_treatment_block(raw: Any) -> dict[str, Any]:
    """
    Parse the treatment block from the raw JSON.

    Args:
        raw: Raw JSON object.

    Returns:
        dict[str, Any]: Parsed treatment dictionary.
    """
    if not isinstance(raw, dict):
        return {"needs": _empty_needs(), "contraindicated_approaches": []}
    parsed_needs = raw.get("needs")
    needs_obj = _empty_needs()
    if isinstance(parsed_needs, dict):
        needs_obj = {
            key: _to_str(parsed_needs.get(key)).strip()
            for key in needs_obj
        }
    return {
        "needs": needs_obj,
        "contraindicated_approaches": [
            _to_str(item).strip()
            for item in (raw.get("contraindicated_approaches") or [])
            if _to_str(item).strip()
        ]
        if isinstance(raw.get("contraindicated_approaches"), list)
        else [],
    }


def _parse_cosmetic_implications_block(raw: Any) -> dict[str, Any]:
    """
    Parse the cosmetic implications block from the raw JSON.
    
    Args:
        raw: Raw JSON object.
        
    Returns:
        dict[str, Any]: Parsed cosmetic implications dictionary.
    """
    if not isinstance(raw, dict):
        return _empty_implications()
    treatment_like = _parse_treatment_block(raw)
    return {
        "needs": treatment_like["needs"],
        "contraindicated_approaches": treatment_like["contraindicated_approaches"],
        "avoid_for_now": [
            _to_str(item).strip()
            for item in (raw.get("avoid_for_now") or [])
            if _to_str(item).strip()
        ]
        if isinstance(raw.get("avoid_for_now"), list)
        else [],
    }


def extract_structured_fields(text: str) -> tuple[dict[str, Any], bool]:
    """
    Parse MedGemma JSON output into normalized legacy + cosmetic fields.

    Args:
        text: Raw JSON text emitted by the model.

    Returns:
        tuple[dict[str, Any], bool]: Parsed fields and whether JSON parsed successfully.
    """
    fields = default_structured_fields()
    try:
        parsed: dict[str, Any] = json.loads(text)
    except Exception:
        return fields, False

    scalar_keys = (
        "schema_version",
        "review_type",
        "diagnosis",
        "primary_observation",
        "rationale",
        "observation_rationale",
        "product_name",
        "description",
        "indications",
        "safety_in_pregnancy",
        "contraindications",
        "how_to_use",
        "special_instructions",
    )
    for key in scalar_keys:
        fields[key] = _to_str(parsed.get(key)).strip()

    fields["summary"] = _parse_summary_block(parsed.get("summary"))
    skin_state_raw = parsed.get("skin_state")
    fields["skin_state"] = (
        _parse_summary_block(skin_state_raw)
        if skin_state_raw is not None
        else dict(fields["summary"])
    )
    fields["treatment"] = _parse_treatment_block(parsed.get("treatment"))
    cosmetic_raw = parsed.get("cosmetic_implications")
    fields["cosmetic_implications"] = (
        _parse_cosmetic_implications_block(cosmetic_raw)
        if cosmetic_raw is not None
        else _parse_cosmetic_implications_block(fields["treatment"])
    )
    fields["risk_tier"] = normalize_risk_tier(_to_str(parsed.get("risk_tier")))
    fields["image_quality"] = normalize_image_quality(_to_str(parsed.get("image_quality")))
    _ensure_skin_diagnosis_populated(fields)

    return fields, True


def _ensure_skin_diagnosis_populated(fields: dict[str, Any]) -> None:
    """
    Guarantee ``diagnosis`` is set for skin/hybrid reviews when the model omits it.

    Args:
        fields: Parsed structured fields (mutated in place).
    """
    review_type = _to_str(fields.get("review_type")).strip().lower()
    if review_type not in {"skin review", "hybrid review"}:
        return

    diagnosis = _to_str(fields.get("diagnosis")).strip()
    if diagnosis:
        return

    primary = _to_str(fields.get("primary_observation")).strip()
    skin_state = fields.get("skin_state")
    if not isinstance(skin_state, dict):
        skin_state = fields.get("summary") if isinstance(fields.get("summary"), dict) else {}
    observed_raw = skin_state.get("observed_patterns") if isinstance(skin_state, dict) else []
    observed = [
        _to_str(item).strip()
        for item in (observed_raw if isinstance(observed_raw, list) else [])
        if _to_str(item).strip()
    ]

    if observed:
        fields["diagnosis"] = ", ".join(observed[:4])
    elif primary:
        fields["diagnosis"] = primary if primary.lower() == "uncertain" else f"likely {primary}"
    else:
        fields["diagnosis"] = "uncertain"


def resolve_skin_observation_text(fields: dict[str, Any]) -> str:
    """
    Return the best available skin observation label from parsed fields.

    Prefers cosmetic ``primary_observation``, then legacy ``diagnosis``.
    Use for user-facing copy only.

    Args:
        fields: Output of :func:`extract_structured_fields`.

    Returns:
        str: Observation text or empty string.
    """
    primary = _to_str(fields.get("primary_observation")).strip()
    if primary:
        return primary
    return _to_str(fields.get("diagnosis")).strip()


def resolve_internal_diagnosis_text(fields: dict[str, Any]) -> str:
    """
    Return the internal clinical hypothesis from parsed fields.

    Prefers ``diagnosis``, then falls back to ``primary_observation`` for legacy
    payloads that only populated diagnosis.

    Args:
        fields: Output of :func:`extract_structured_fields`.

    Returns:
        str: Internal diagnosis text or empty string.
    """
    diagnosis = _to_str(fields.get("diagnosis")).strip()
    if diagnosis:
        return diagnosis
    return _to_str(fields.get("primary_observation")).strip()


def resolve_confidence_span_target(parsed: dict[str, Any]) -> tuple[str, str]:
    """
    Choose the JSON field and text span used for vision confidence logprobs.

    Prefers internal ``diagnosis`` when populated so confidence tracks the
    clinical hypothesis used for escalation and research.

    Args:
        parsed: Parsed model JSON object.

    Returns:
        tuple[str, str]: ``(field_text, json_field_name)`` for span alignment.
    """
    diagnosis = _to_str(parsed.get("diagnosis")).strip()
    if diagnosis:
        return diagnosis, "diagnosis"
    primary = _to_str(parsed.get("primary_observation")).strip()
    if primary:
        return primary, "primary_observation"
    return "", ""

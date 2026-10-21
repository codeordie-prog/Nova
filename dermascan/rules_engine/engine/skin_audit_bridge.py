"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-08-07
License: MIT

Build SkinAudit bridge objects from legacy and cosmetic MedGemma fields.
"""


from __future__ import annotations

import json
from typing import Any, Mapping

from dermascan.ai_service.postprocessing.medgemma_structured_output import (
    normalize_image_quality,
    normalize_risk_tier,
    resolve_skin_observation_text,
)
from dermascan.rules_engine.engine.engine import (
    RulesEngineRequest,
    SkinAudit,
    SkinCurrentCondition,
    SkinNeeds,
)


def _parse_raw_payload(request: RulesEngineRequest) -> dict[str, Any]:
    """
    Parse optional raw MedGemma JSON attached to a rules-engine request.

    Args:
        request: Rules engine request.

    Returns:
        dict[str, Any]: Parsed payload or empty dict.
    """
    raw = str(request.raw_model_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_primary_concern(observation: str) -> str:
    """
    Normalize an observation label to a single primary concern token.

    Args:
        observation: Raw observation or diagnosis text.

    Returns:
        str: Primary concern string.
    """
    raw = str(observation or "").strip()
    if not raw:
        return ""
    first = raw.split(";", 1)[0]
    first = first.split(",", 1)[0]
    first = first.split("(", 1)[0]
    return first.strip()


def _parse_bool_like(value: Any) -> bool | None:
    """Parse bool-like strings into booleans."""
    token = str(value or "").strip().lower()
    if token in {"true", "yes", "1"}:
        return True
    if token in {"false", "no", "0"}:
        return False
    return None


def _coerce_treatment_dict(value: Mapping[str, Any]) -> str:
    """
    Serialize a treatment/implications dict to the bridge string format.

    Args:
        value: Treatment or cosmetic_implications mapping.

    Returns:
        str: Deterministic treatment string for needs parsing.
    """
    needs_raw = value.get("needs") if isinstance(value.get("needs"), dict) else {}
    needs = {
        "barrier_repair": str(needs_raw.get("barrier_repair") or "").strip(),
        "anti_inflammatory": str(needs_raw.get("anti_inflammatory") or "").strip(),
        "comedolytic": str(needs_raw.get("comedolytic") or "").strip(),
        "hydration": str(needs_raw.get("hydration") or "").strip(),
        "sun_protection": str(needs_raw.get("sun_protection") or "").strip(),
    }
    needs_text = ",".join(f"{key}:{needs[key]}" for key in needs)
    contraindicated_raw = value.get("contraindicated_approaches")
    contraindicated: list[str] = []
    if isinstance(contraindicated_raw, list):
        contraindicated = [str(item).strip() for item in contraindicated_raw if str(item).strip()]
    contraindicated_text = "|".join(contraindicated) if contraindicated else "none"
    return f"needs={needs_text};contraindicated_approaches={contraindicated_text}"


def _resolve_summary_dict(request: RulesEngineRequest) -> dict[str, Any]:
    """
    Resolve the best available structured summary block for bridge parsing.

    Args:
        request: Rules engine request.

    Returns:
        dict[str, Any]: Structured summary/skin_state mapping (may be empty).
    """
    payload = _parse_raw_payload(request)
    skin_state = payload.get("skin_state")
    if isinstance(skin_state, dict):
        return skin_state

    summary = payload.get("summary")
    if isinstance(summary, dict):
        return summary

    request_summary = str(request.summary or "").strip()
    if request_summary.startswith("{"):
        try:
            parsed = json.loads(request_summary)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _resolve_implications_dict(request: RulesEngineRequest) -> dict[str, Any]:
    """
    Resolve cosmetic implications or legacy treatment mapping from the request.

    Args:
        request: Rules engine request.

    Returns:
        dict[str, Any]: Structured implications/treatment mapping (may be empty).
    """
    payload = _parse_raw_payload(request)
    implications = payload.get("cosmetic_implications")
    if isinstance(implications, dict):
        return implications

    treatment = payload.get("treatment")
    if isinstance(treatment, dict):
        return treatment

    request_treatment = str(request.treatment or "").strip()
    if request_treatment.startswith("{"):
        try:
            parsed = json.loads(request_treatment)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def extract_current_condition_from_dict(summary: Mapping[str, Any]) -> SkinCurrentCondition:
    """
    Build SkinCurrentCondition from structured summary/skin_state JSON.

    Args:
        summary: Structured summary mapping.

    Returns:
        SkinCurrentCondition: Parsed skin condition signals.
    """
    condition = SkinCurrentCondition()
    if not summary:
        return condition

    severity = str(summary.get("severity") or "").strip().lower()
    if severity in {"mild", "moderate", "severe"}:
        condition.severity = severity

    inflammation = _parse_bool_like(summary.get("active_inflammation"))
    if inflammation is True:
        condition.active_inflammation = True

    barrier_status = str(summary.get("barrier_status") or "").strip().lower()
    if barrier_status in {"compromised", "intact"}:
        condition.barrier_status = barrier_status

    patterns = summary.get("observed_patterns")
    if isinstance(patterns, list):
        condition.observed_patterns = [
            str(item).strip() for item in patterns if str(item).strip()
        ]

    return condition


def extract_current_condition_from_text(summary: str) -> SkinCurrentCondition:
    """
    Infer current condition signals from legacy coerced summary text.

    Args:
        summary: Coerced summary string.

    Returns:
        SkinCurrentCondition: Parsed skin condition signals.
    """
    text = str(summary or "").strip().lower()
    condition = SkinCurrentCondition()
    if not text:
        return condition

    if "severity=severe" in text or "severe" in text:
        condition.severity = "severe"
    elif "severity=moderate" in text or "moderate" in text:
        condition.severity = "moderate"
    elif "severity=mild" in text or "mild" in text:
        condition.severity = "mild"

    if "active_inflammation=true" in text:
        condition.active_inflammation = True

    if "barrier_status=compromised" in text or "compromised barrier" in text:
        condition.barrier_status = "compromised"
    elif "barrier_status=intact" in text or "barrier intact" in text:
        condition.barrier_status = "intact"

    pattern_markers = (
        "papule",
        "pustule",
        "comedone",
        "scaling",
        "patch",
        "hyperpigment",
        "erythema",
        "redness",
        "bumps",
    )
    condition.observed_patterns = [marker for marker in pattern_markers if marker in text]
    return condition


def resolve_observation_text(request: RulesEngineRequest) -> str:
    """
    Resolve the best skin observation label for bridge construction.

    Args:
        request: Rules engine request.

    Returns:
        str: Primary observation or diagnosis text.
    """
    explicit = str(request.primary_observation or "").strip()
    if explicit:
        return explicit

    payload = _parse_raw_payload(request)
    if payload:
        return resolve_skin_observation_text(payload)

    return str(request.diagnosis or "").strip()


def resolve_risk_tier(request: RulesEngineRequest) -> str:
    """
    Resolve risk tier from request fields or raw payload.

    Args:
        request: Rules engine request.

    Returns:
        str: Normalized risk tier.
    """
    explicit = str(request.risk_tier or "").strip()
    if explicit:
        return normalize_risk_tier(explicit)

    payload = _parse_raw_payload(request)
    return normalize_risk_tier(str(payload.get("risk_tier") or ""))


def resolve_image_quality(request: RulesEngineRequest) -> str:
    """
    Resolve image quality from request fields or raw payload.

    Args:
        request: Rules engine request.

    Returns:
        str: Normalized image quality label.
    """
    explicit = str(request.image_quality or "").strip()
    if explicit and explicit != "unknown":
        return normalize_image_quality(explicit)

    payload = _parse_raw_payload(request)
    return normalize_image_quality(str(payload.get("image_quality") or ""))


def resolve_treatment_text(request: RulesEngineRequest) -> str:
    """
    Resolve treatment/implications as bridge-compatible text.

    Args:
        request: Rules engine request.

    Returns:
        str: Treatment string used by needs derivation.
    """
    implications = _resolve_implications_dict(request)
    if implications:
        return _coerce_treatment_dict(implications)
    return str(request.treatment or "").strip()


def resolve_avoid_for_now(request: RulesEngineRequest) -> list[str]:
    """
    Extract avoid_for_now guidance strings from cosmetic implications.

    Args:
        request: Rules engine request.

    Returns:
        list[str]: Cosmetic avoid guidance strings.
    """
    implications = _resolve_implications_dict(request)
    raw = implications.get("avoid_for_now")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def build_skin_audit_bridge(
    request: RulesEngineRequest,
    *,
    derive_skin_needs: Any,
    score_skin_audit_completeness: Any,
) -> SkinAudit:
    """
    Build a SkinAudit bridge from legacy and cosmetic MedGemma fields.

    Args:
        request: Rules engine request containing flat and raw payload fields.
        derive_skin_needs: Callable mirroring RulesEngine._derive_skin_needs.
        score_skin_audit_completeness: Callable mirroring completeness scorer.

    Returns:
        SkinAudit: Bridge object for downstream product/skin enrichment.
    """
    observation = resolve_observation_text(request)
    concern = _extract_primary_concern(observation)

    summary_dict = _resolve_summary_dict(request)
    if summary_dict:
        condition = extract_current_condition_from_dict(summary_dict)
    else:
        condition = extract_current_condition_from_text(request.summary)

    treatment_text = resolve_treatment_text(request)
    needs: SkinNeeds = derive_skin_needs(
        concern=concern,
        treatment=treatment_text,
        condition=condition,
    )
    completeness_score, missing_fields = score_skin_audit_completeness(
        concern=concern,
        condition=condition,
        needs=needs,
    )

    contraindicated_approaches: list[str] = []
    implications = _resolve_implications_dict(request)
    raw_contraindicated = implications.get("contraindicated_approaches")
    if isinstance(raw_contraindicated, list):
        contraindicated_approaches.extend(
            str(item).strip() for item in raw_contraindicated if str(item).strip()
        )

    avoid_for_now = resolve_avoid_for_now(request)
    for item in avoid_for_now:
        if item not in contraindicated_approaches:
            contraindicated_approaches.append(item)

    if condition.active_inflammation and needs.comedolytic:
        heuristic = "avoid aggressive actives while inflamed"
        if heuristic not in contraindicated_approaches:
            contraindicated_approaches.append(heuristic)
    if condition.barrier_status == "compromised":
        heuristic = "avoid barrier-stripping routines"
        if heuristic not in contraindicated_approaches:
            contraindicated_approaches.append(heuristic)

    confidence = max(0.0, min(1.0, float(request.skin_audit.confidence or 0.0)))
    return SkinAudit(
        primary_concern=concern,
        current_condition=condition,
        needs=needs,
        contraindicated_approaches=contraindicated_approaches,
        confidence=confidence,
        completeness_score=completeness_score,
        missing_fields=missing_fields,
        risk_tier=resolve_risk_tier(request),
        image_quality=resolve_image_quality(request),
        avoid_for_now=avoid_for_now,
    )

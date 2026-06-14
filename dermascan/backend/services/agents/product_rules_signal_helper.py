"""
Author: Dermascan Team
Date: 6-14-2026
Licence: MIT

Helpers for product-review signal enrichment via ingredient extraction + rules engine."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from dermascan.backend.services.agents.provider_circuit_breaker import load_qwen_circuit_breaker
from dermascan.rules_engine.engine.engine import RulesEngineRequest
from dermascan.rules_engine.engine.rules_engine import RulesEngine
from dermascan.shared.logger import get_logger
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries

load_dotenv()

logger = get_logger(__name__)

TAVILY_API_KEY = str(os.getenv("TAVILY_API_KEY_QUTIS", ""))
_PROVIDER_MAX_RETRIES_HARD_CAP = 3
QWEN_API_KEY = str(os.getenv("QWEN_API_KEY") or os.getenv("QWEN_OPENAI_API_KEY") or "EMPTY")
QWEN_BASE_URL = str(os.getenv("QWEN_BASE_URL") or os.getenv("QWEN_OPENAI_BASE_URL") or "")
QWEN_MODEL_NAME = str(os.getenv("QWEN_SERVED_MODEL_NAME") or os.getenv("QWEN_MODEL_NAME") or "qwen-3-8b")
QWEN_REQUEST_TIMEOUT_S = float(os.getenv("QWEN_REQUEST_TIMEOUT_S", "45"))
QWEN_MAX_RETRIES = max(0, min(_PROVIDER_MAX_RETRIES_HARD_CAP, int(os.getenv("QWEN_MAX_RETRIES", "2"))))
QWEN_ENABLE_THINKING = os.getenv("QWEN_ENABLE_THINKING", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
_SKIN_AUDIT_KEY_TEMPLATE = "client:{client_id}:skin_profile"
_SKIN_AUDIT_TTL_S = max(60, int(os.getenv("SKIN_AUDIT_TTL_S", str(60 * 60 * 24 * 30))))

class IngredientExtractionResponse(BaseModel):
    """Structured output from product ingredient extraction agent."""

    ingredients: list[str] = Field(default_factory=list)
    notes: str = ""
    sources: list[str] = Field(default_factory=list)


class UserFriendlyReportResponse(BaseModel):
    """User-friendly final report for outbound delivery."""

    report_text: str = ""


class SkinReviewResponse(BaseModel):
    """Structured output from skin-review research agent."""

    report_text: str = ""

# Global rules engine instance + Model circuit breaker
_rules_engine_singleton: RulesEngine | None = None
_qwen_breaker = load_qwen_circuit_breaker()


def _build_qwen_model() -> ChatOpenAI:
    """
    Build a Qwen ChatOpenAI client using the shared OpenAI-compatible endpoint.
    
    Returns:
        ChatOpenAI instance - an openai compatible API for the self-hosted model
    """
    base_url = QWEN_BASE_URL.rstrip("/")
    if not base_url:
        raise RuntimeError("QWEN_BASE_URL (or QWEN_OPENAI_BASE_URL) is required for subagents")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    return ChatOpenAI(
        model=QWEN_MODEL_NAME,
        api_key=QWEN_API_KEY,
        base_url=base_url,
        timeout=QWEN_REQUEST_TIMEOUT_S,
        max_retries=QWEN_MAX_RETRIES,
        temperature=0,
        extra_body={"chat_template_kwargs": {"enable_thinking": QWEN_ENABLE_THINKING}},
    )


def _should_retry_provider_error(exc: Exception) -> bool:
    """
    Return True for transient provider errors that are safe to retry.
    
    Args:
        exc (Exception): The exception.


    Returns:
        bool - True if should be retried else false.
    """
    text = str(exc).lower()
    markers = (
        "429",
        "rate limit",
        "resource exhausted",
        "timeout",
        "timed out",
        "connection",
        "connect error",
        "service unavailable",
        "temporarily unavailable",
        "bad gateway",
        "gateway timeout",
        "502",
        "503",
        "504",
    )
    return any(marker in text for marker in markers)


def _invoke_with_retry(*, agent: Any, prompt: str, operation: str) -> dict[str, Any]:
    """
    Invoke an agent with circuit-breaker guarding and bounded retries.
    
    Args:
        Agent (Any): Agent instance.
        prompt (str): The prompt for the agent.
        operation (str): The operation carried out [skin or product review research]
    
    Returns:
        dict (str, Any)
    """
    max_attempts = max(1, QWEN_MAX_RETRIES + 1)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        if not _qwen_breaker.allow():
            raise RuntimeError(f"qwen circuit open; skipping {operation}")
        try:
            response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
            _qwen_breaker.record_success()
            return response if isinstance(response, dict) else {}
        except Exception as exc:
            last_exc = exc
            _qwen_breaker.record_failure()
            if attempt >= max_attempts or not _should_retry_provider_error(exc):
                raise
            logger.warning(
                "%s attempt %s/%s failed; retrying: %s",
                operation,
                attempt,
                max_attempts,
                exc,
            )

    raise last_exc if last_exc is not None else RuntimeError(f"{operation} failed")


def _normalize_inci_name(raw_name: str) -> str:
    """
    Normalize INCI name
    Args:
        raw_name (str): raw ingredient name

    Returns:
        str: Normalized name
    """
    cleaned = str(raw_name or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\([^)]*\)", "", cleaned)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*%\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    return cleaned


def _safe_json_loads(raw: str) -> dict[str, Any]:
    """
    Safely parse JSON string
    
    Args:
        raw (str): JSON string
        
    Returns:
        dict[str, Any]: Parsed JSON or empty dict
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _skin_audit_key(client_id: str) -> str:
    """
    Build Redis key for latest client skin audit.
    Args:
        client_id (str): The client ID

    Returns:
        str: The Redis key
    """
    normalized_client_id = str(client_id or "anonymous").strip() or "anonymous"
    return _SKIN_AUDIT_KEY_TEMPLATE.format(client_id=normalized_client_id)


def _load_latest_skin_audit(client_id: str) -> tuple[dict[str, Any], int | None]:
    """
    Load latest persisted skin audit (+timestamp) from Redis for a client.
    Args:
        client_id (str): The client ID

    Returns:
        tuple[dict[str, Any], int | None]: The latest skin audit and timestamp
    """
    key = _skin_audit_key(client_id)
    try:
        redis_client = get_redis_client(decode_responses=True)
        raw_payload = run_with_redis_retries(
            lambda: redis_client.get(key),
            operation_name="skin_audit_get",
        )
    except Exception as exc:
        logger.warning("failed to load skin audit from redis: client_id=%s err=%s", client_id, exc)
        return {}, None

    if not raw_payload:
        return {}, None
    parsed = _safe_json_loads(str(raw_payload))
    if not isinstance(parsed, dict):
        return {}, None

    if isinstance(parsed.get("skin_audit"), dict):
        updated_at_ts_raw = parsed.get("updated_at_ts")
        try:
            updated_at_ts = int(updated_at_ts_raw) if updated_at_ts_raw is not None else None
        except Exception:
            updated_at_ts = None
        return parsed["skin_audit"], updated_at_ts

    return parsed, None


def _persist_latest_skin_audit(client_id: str, skin_audit: Any) -> None:
    """
    Persist latest skin audit in Redis with TTL.
    Args:
        client_id (str): The client ID
        skin_audit (Any): The skin audit

    Returns:
        None
    """
    if isinstance(skin_audit, BaseModel):
        payload = skin_audit.model_dump()
    elif isinstance(skin_audit, dict):
        payload = skin_audit
    else:
        payload = {}

    if not payload:
        return

    key = _skin_audit_key(client_id)
    serialized = json.dumps(
        {
            "skin_audit": payload,
            "updated_at_ts": int(time.time()),
        },
        default=str,
    )
    try:
        redis_client = get_redis_client(decode_responses=True)
        run_with_redis_retries(
            lambda: redis_client.set(key, serialized, ex=_SKIN_AUDIT_TTL_S),
            operation_name="skin_audit_set",
        )
    except Exception as exc:
        logger.warning("failed to persist skin audit to redis: client_id=%s err=%s", client_id, exc)


def _coerce_summary_payload(value: Any) -> str:
    """
    Normalize summary payload into a deterministic string representation.
    Args:
        value (Any): The summary payload

    Returns:
        str: The normalized summary payload
    """
    if isinstance(value, dict):
        severity = str(value.get("severity") or "").strip()
        active_inflammation = str(value.get("active_inflammation") or "").strip()
        barrier_status = str(value.get("barrier_status") or "").strip()
        observed_raw = value.get("observed_patterns")
        observed: list[str] = []
        if isinstance(observed_raw, list):
            observed = [str(item).strip() for item in observed_raw if str(item).strip()]
        observed_patterns = "|".join(observed) if observed else "none"
        return (
            f"severity={severity};"
            f"active_inflammation={active_inflammation};"
            f"barrier_status={barrier_status};"
            f"observed_patterns={observed_patterns}"
        )
    return str(value or "").strip()


def _coerce_treatment_payload(value: Any) -> str:
    """
    Normalize treatment payload into a deterministic string representation.
    Args:
        value (Any): The treatment payload

    Returns:
        str: The normalized treatment payload
    """
    if isinstance(value, dict):
        needs_raw = value.get("needs") if isinstance(value.get("needs"), dict) else {}
        needs = {
            "barrier_repair": str(needs_raw.get("barrier_repair") or "").strip(),
            "anti_inflammatory": str(needs_raw.get("anti_inflammatory") or "").strip(),
            "comedolytic": str(needs_raw.get("comedolytic") or "").strip(),
            "hydration": str(needs_raw.get("hydration") or "").strip(),
            "sun_protection": str(needs_raw.get("sun_protection") or "").strip(),
        }
        needs_text = ",".join(f"{k}:{v}" for k, v in needs.items())
        contraindicated_raw = value.get("contraindicated_approaches")
        contraindicated: list[str] = []
        if isinstance(contraindicated_raw, list):
            contraindicated = [str(item).strip() for item in contraindicated_raw if str(item).strip()]
        contraindicated_text = "|".join(contraindicated) if contraindicated else "none"
        return f"needs={needs_text};contraindicated_approaches={contraindicated_text}"
    return str(value or "").strip()


def _strip_markdown_formatting(text: str) -> str:
    """
    Strip down Markdown formating.

    Args:
        text (str): text to strip

    Returns:
        str: cleaned up string.
    """
    cleaned = str(text or "")
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


@tool
def inci_decoder_search(query: str, max_results: int = 8) -> str:
    """
    Search ingredient/product references restricted to INCI Decoder domains.

    Args:
        query (str): The search query
        max_results (int): Maximum number of results

    Returns:
        str: Search results
    """
    if not TAVILY_API_KEY:
        return "Tavily is not configured."
    client = TavilyClient(api_key=TAVILY_API_KEY)
    try:
        result = client.search(
            query=query,
            max_results=max(1, min(max_results, 10)),
            search_depth="advanced",
            include_domains=["incidecoder.com"],
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return f"INCI decoder search failed: {exc}"


@tool
def skin_review_search(query: str, max_results: int = 4) -> str:
    """
    Internet search for dermatology and skin condition information from reputable medical sources.
    Use this tool to strengthen the skin audit report.

    Args:
        query (str): The search query
        max_results (int): Maximum number of results

    Returns:
        str: Search results
    """
    if not TAVILY_API_KEY:
        return "Tavily is not configured."
    client = TavilyClient(api_key=TAVILY_API_KEY)
    try:
        result = client.search(
            query=query,
            max_results=max(1, min(max_results, 10)),
            search_depth="advanced",
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return f"Skin review search failed: {exc}"


def _build_ingredient_agent() -> Any:
    """
    Build a simple subagent for ingredient search.
    This agent searches the ingredients of a given product from INCI decoder.
    These ingredient names are then normalized and used to query our safety database.

    """
    model = _build_qwen_model()
    return create_agent(
        model=model,
        tools=[inci_decoder_search],
        response_format=IngredientExtractionResponse,
        system_prompt=(
            "You extract cosmetic product ingredient names in strict INCI-style naming. "
            "Always use the inci_decoder_search tool before finalizing when product identity is available. "
            "Return only ingredient names without concentration or percentages. "
            "Example: 'Niacinamide 10%' -> 'Niacinamide'. "
            "Never return explanatory prose in ingredients; keep it clean and deduplicated."
            "Return at-most the first 10 main ingredients."
            "Avoid formatting your responses, reply with a clean well structured response, No bold or italics."
        ),
    )


def extract_product_ingredients(*, product_name: str, description: str) -> IngredientExtractionResponse:
    """
    Extract product ingredients.
    Args:
        product_name (str): The name of the product
        description (str): The description of the product
    
    Returns:
        IngredientExtractionResponse: The extracted ingredients
    """
    # Build agent
    agent = _build_ingredient_agent()
    prompt = (
        "Extract the ingredient list for this cosmetic product.\n"
        "Use INCI names only and remove concentration values.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
    )
    try:
        response = _invoke_with_retry(
            agent=agent,
            prompt=prompt,
            operation="ingredient_extraction",
        )
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, IngredientExtractionResponse):
            ingredients = [_normalize_inci_name(item) for item in structured.ingredients]
            ingredients = [item for item in ingredients if item]
            return IngredientExtractionResponse(
                ingredients=list(dict.fromkeys(ingredients)),
                notes=structured.notes,
                sources=structured.sources,
            )
    except Exception:
        logger.exception("failed to extract product ingredients")

    return IngredientExtractionResponse()


def get_rules_engine() -> RulesEngine:
    """
    Initialize Rules Engine with default values (vector store and redis client).

    Returns:
        RulesEngine instance
    """
    global _rules_engine_singleton
    if _rules_engine_singleton is None:
        _rules_engine_singleton = RulesEngine()
    return _rules_engine_singleton

@tool
def report_internet_search(query: str, max_results: int = 8) -> str:
    """
    Search the internet for information to help with the report.
    Args:
        query (str): The search query
        max_results (int): Maximum number of results

    Returns:
        str: Search results
    """
    if not TAVILY_API_KEY:
        return "Tavily is not configured."
    client = TavilyClient(api_key=TAVILY_API_KEY)
    try:
        result = client.search(
            query=query,
            max_results=max(1, min(max_results, 10)),
            search_depth="advanced",
            include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov"],
        )
        return json.dumps(result, default=str)
    except Exception as exc:
        return f"Report internet search failed: {exc}" 

def _build_report_agent(context: list[dict[str, Any]]) -> Any:
    """
    Build a report summarization subagent.
    
    """
    model = _build_qwen_model()
    return create_agent(
        model=model,
        tools=[report_internet_search],
        response_format=UserFriendlyReportResponse,
        system_prompt=(
            "You are a dermatology-focused product safety communication assistant. "
            "Convert technical safety evidence into a concise, user-friendly report. "
            "Use plain language, avoid definitive medical diagnosis claims, and include practical caution advice."
            "Each ingredient should have its own section in the report."
            "Ensure the report is comprehensive and covers all the ingredients."
            "Ensure the recommended concentration of use is mentioned for each ingredient."
            "Report on interactions with other products when possible."
            "Use the last_conversation_entries to provide context for the report."
            "When reviewing the product, the last_conversation_entries will help you structure your response to the user's query."
            "Always prioritize, safety of the ingredients, pregnancy and lactation, recommended concentrations, compatibility with user's skin or other product they are using, any other relevant information."
            f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        ),
    )

def _build_skin_review_agent(context: list[dict[str, Any]]) -> Any:
    """
    Build a skin review agent
    """
    model = _build_qwen_model()

    return create_agent(
        model=model,
        tools=[skin_review_search],
        response_format=SkinReviewResponse,
        system_prompt=(
            "You are a dermatology skin-audit assistant. "
            "Your task is to produce a concise user-facing skin audit from the provided primary concern, condition summary, and needs/treatment context. "
            "Use the skin_review_search tool to strengthen evidence when useful. "
            "Return plain text only; do not use markdown, bold, or italics. "
            "Do not prescribe medications, dosages, or definitive treatment plans. "
            "Do not claim definitive diagnosis; use cautious language such as 'consistent with'. "
            "The output must be structured in this order: "
            "Primary concern; Condition snapshot; Likely contributors; Care priorities (general, non-prescriptive); Cautions and when to seek in-person care; Safety disclaimer. "
            "Keep it practical, short, and aligned with conversation context. "
            f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        )
    )


def build_skin_review_report(
    *,
    diagnosis: str,
    summary: str,
    treatment: str,
    internet_research: str,
    context: list[dict[str, Any]],
) -> str:
    """Build a concise user-facing skin review report."""
    agent = _build_skin_review_agent(context)
    prompt = (
        "Generate the final skin audit using the inputs below.\n"
        "If a field is missing, state 'unknown' briefly and continue.\n"
        f"primary_concern: {diagnosis}\n"
        f"skin_condition_summary: {summary}\n"
        f"needs_and_treatment: {treatment}\n"
        f"internet_research_on_primary_concern: {internet_research}\n"
    )
    try:
        response = _invoke_with_retry(
            agent=agent,
            prompt=prompt,
            operation="skin_review_report",
        )
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, SkinReviewResponse) and structured.report_text.strip():
            return _strip_markdown_formatting(structured.report_text)
    except Exception:
        logger.exception("failed to build skin review report")

    return "Skin research completed. This information is educational and not a medical diagnosis or prescription."

def build_user_friendly_report(
    *,
    product_name: str,
    description: str,
    ingredients: list[str],
    report: dict[str, Any],
    sources: list[str],
    context: list[dict[str, Any]],
) -> str:
    """
    Build a user-friendly report from the rules engine report.
    
    Args:
        product_name: Name of the product
        description: Product description
        ingredients: List of ingredients
        report: Rules engine report
        sources: List of sources
        
    Returns:
        User-friendly report as string
    """
    agent = _build_report_agent(context)
    prompt = (
        "Write a user-friendly safety report for this cosmetic product.\n"
        "Keep it short, clear, and practical for a non-technical user.\n"
        "Include: overview, key safety points, contraindication highlights, and what to do next.\n"
        "Avoid formatting your repsonses. No bolds or italics.\n"
        "Use the last_conversation_entries to provide context for the report.\n"
        "When reviewing the product, the last_conversation_entries will help you structure your response to the user's query."
        "Always prioritize, safety of the ingredients, pregnancy and lactation, compatibility with user's skin or other product they are using, any other relevant information."
        f"product_name: {product_name}\n"
        f"description: {description}\n"
        f"ingredients: {json.dumps(ingredients, default=str)}\n"
        f"rules_engine_report_json: {json.dumps(report, default=str)}\n"
        f"sources: {json.dumps(sources, default=str)}\n"
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
    )
    try:
        response = _invoke_with_retry(
            agent=agent,
            prompt=prompt,
            operation="product_user_friendly_report",
        )
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, UserFriendlyReportResponse) and structured.report_text.strip():
            return _strip_markdown_formatting(structured.report_text)
    except Exception:
        logger.exception("failed to build user-friendly product report")

    return "Safety review completed. Please consult the detailed report and follow product caution guidance."


def run_product_signal_rules_flow(fields: dict[str, Any], context: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Run the product signal rules flow.
    
    Args:
        fields: Dictionary containing request fields
        
    Returns:
        Dictionary containing the result of the flow
    """
    payload = _safe_json_loads(str(fields.get("payload") or "{}"))
    request_id = str(fields.get("request_id") or "")
    case_id = str(fields.get("case_id") or request_id)
    client_id = str(fields.get("client_id") or "anonymous")
    message_id = str(fields.get("message_id") or request_id)
    channel = str(fields.get("channel") or "whatsapp")

    product_name = str(payload.get("product_name") or "").strip()
    description = str(payload.get("description") or "").strip()

    latest_skin_audit, _ = _load_latest_skin_audit(client_id)

    logger.info(
        "product signal rules flow started: request_id=%s case_id=%s client_id=%s product_name=%s",
        request_id,
        case_id,
        client_id,
        product_name or "<empty>",
    )

    extraction = extract_product_ingredients(product_name=product_name, description=description)
    logger.info(
        "ingredient extraction completed: request_id=%s ingredients_count=%s ingredients=%s",
        request_id,
        len(extraction.ingredients),
        extraction.ingredients,
    )

    rules_engine = get_rules_engine()
    request = RulesEngineRequest(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        message_id=message_id,
        channel=channel,
        review_type="product",
        ingredients=extraction.ingredients,
        skin_audit=latest_skin_audit,
        product_name=product_name,
        description=description,
        indications=str(payload.get("indications") or ""),
        safety_in_pregnancy=str(payload.get("safety_in_pregnancy") or ""),
        contraindications=str(payload.get("contraindications") or ""),
        how_to_use=str(payload.get("how_to_use") or ""),
        special_instructions=str(payload.get("special_instructions") or ""),
        raw_model_json=str(fields.get("payload") or ""),
    )

    logger.info("rules engine request created: request_id=%s", request_id)
    result = rules_engine.process(request)
    logger.info(
        "rules engine completed: request_id=%s indexed_ingredients=%s",
        request_id,
        len(result.indexed_ingredients),
    )

    user_friendly_report = build_user_friendly_report(
        product_name=product_name,
        description=description,
        ingredients=extraction.ingredients,
        report=result.report,
        sources=result.sources,
        context=context,
    )

    return {
        "summary": result.summary,
        "user_friendly_report": user_friendly_report,
        "sources": result.sources,
        "ingredients": extraction.ingredients,
        "indexed_ingredients": [item.model_dump() for item in result.indexed_ingredients],
        "report": result.report,
    }


def run_skin_signal_rules_flow(fields: dict[str, Any], context: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the skin signal rules flow."""
    payload = _safe_json_loads(str(fields.get("payload") or "{}"))
    request_id = str(fields.get("request_id") or "")
    case_id = str(fields.get("case_id") or request_id)
    client_id = str(fields.get("client_id") or "anonymous")
    message_id = str(fields.get("message_id") or request_id)
    channel = str(fields.get("channel") or "whatsapp")

    diagnosis = str(payload.get("diagnosis") or payload.get("diagnosis_text") or "").strip()
    summary = _coerce_summary_payload(payload.get("summary"))
    treatment = _coerce_treatment_payload(payload.get("treatment"))

    logger.info(
        "skin signal rules flow started: request_id=%s case_id=%s client_id=%s diagnosis=%s",
        request_id,
        case_id,
        client_id,
        diagnosis or "<empty>",
    )

    rules_engine = get_rules_engine()
    request = RulesEngineRequest(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        message_id=message_id,
        channel=channel,
        review_type="skin",
        diagnosis=diagnosis,
        summary=summary,
        treatment=treatment,
        raw_model_json=str(fields.get("payload") or ""),
    )

    logger.info("skin rules engine request created: request_id=%s", request_id)
    result = rules_engine.process(request)

    report = result.report if isinstance(result.report, dict) else {}
    _persist_latest_skin_audit(client_id, report.get("skin_audit"))
    user_friendly_report = build_skin_review_report(
        diagnosis=str(report.get("diagnosis") or diagnosis),
        summary=str(report.get("summary") or summary),
        treatment=str(report.get("treatment") or treatment),
        internet_research=str(report.get("internet_research") or ""),
        context=context,
    )

    return {
        "summary": result.summary,
        "user_friendly_report": user_friendly_report,
        "sources": result.sources,
        "report": result.report,
    }

if __name__ == "__main__":

    res = extract_product_ingredients(product_name="La Roche-Posay Anthelios Oil Control Fluid Uvmune 400 SPF50 Sunscreen+ 50ml", description="Anti-ageing")
    print(res)
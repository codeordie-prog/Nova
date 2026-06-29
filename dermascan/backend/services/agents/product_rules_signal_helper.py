"""
Author: Dermascan Team
Date: 6-14-2026
Licence: MIT

Helpers for product-review signal enrichment via ingredient extraction + rules engine."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from dermascan.backend.services.agents.build import build_gemini_chat_model
from dermascan.backend.services.agents.provider_circuit_breaker import (
    CircuitBreaker,
    load_gemini_circuit_breaker,
    load_openai_circuit_breaker,
)
from dermascan.rules_engine.engine.engine import RulesEngineRequest, SkinContext
from dermascan.rules_engine.engine.rules_engine import RulesEngine
from dermascan.shared.logger import get_logger
from dermascan.shared.langsmith_observability import (
    build_invoke_config,
    configure_worker_project,
)
from dermascan.shared.stream_pipeline_tracing import (
    extract_confidence_from_fields,
    extract_threshold_from_fields,
)
from dermascan.shared.delivery_context import (
    DeliveryContext,
    build_rules_skin_context,
    format_delivery_context_for_prompt,
    load_delivery_context,
    merge_profile_into_skin_audit,
)
from dermascan.shared.redis_stability import get_redis_client, run_with_redis_retries
from langchain_core.runnables import RunnableConfig

# Load env variables
load_dotenv()

# Configure worker project
configure_worker_project("signal_rules")

# Initialize logger
logger = get_logger(__name__)

TAVILY_API_KEY = str(os.getenv("TAVILY_API_KEY_QUTIS", ""))
_PROVIDER_MAX_RETRIES_HARD_CAP = 3
OPENAI_API_KEY = str(os.getenv("OPENAI_API_KEY_QUTIS", ""))
OPENAI_MODEL_NAME = str(os.getenv("ORCHESTRATOR_OPENAI_MODEL") or os.getenv("OPENAI_MODEL_NAME") or "gpt-4o")
OPENAI_REQUEST_TIMEOUT_S = float(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "45"))
OPENAI_MAX_RETRIES = max(0, min(_PROVIDER_MAX_RETRIES_HARD_CAP, int(os.getenv("OPENAI_MAX_RETRIES", "2"))))
GEMINI_MODEL_NAME = str(os.getenv("REPORT_AGENT_MODEL", "gemini-3.5-flash"))
GEMINI_THINKING_LEVEL = str(os.getenv("ORCHESTRATOR_GEMINI_THINKING_LEVEL", "minimal"))
ORCHESTRATOR_MAX_TOKENS = max(256, int(os.getenv("ORCHESTRATOR_MAX_TOKENS", "2048")))
GEMINI_API_KEY = str(os.getenv("GEMINI_API_KEY_QUTIS", ""))
GOOGLE_CREDENTIALS_PATH = str(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_QUTIS", ""))
GOOGLE_PROJECT_ID = str(
    os.getenv("GOOGLE_CLOUD_PROJECT_QUTIS")
    or os.getenv("GOOGLE_PROJECT_ID_QUTIS", "qutisai")
)
GOOGLE_LOCATION = str(os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
_SKIN_AUDIT_KEY_TEMPLATE = "skin_profile:{client_id}"
_REPORT_MAX_SOURCES = max(1, int(os.getenv("SIGNAL_RULES_MAX_SOURCES", "2")))

# Define response models
class IngredientExtractionResponse(BaseModel):
    """Structured response model for extracted ingredients from the product ingredient extraction agent."""

    ingredients: list[str] = Field(default_factory=list)
    notes: str = ""
    sources: list[str] = Field(default_factory=list)


class UserFriendlyReportResponse(BaseModel):
    """User-friendly final report for outbound delivery."""

    report_text: str = ""


class SkinReviewResponse(BaseModel):
    """Structured output from skin-review research agent."""

    report_text: str = ""

# Global rules engine instance + provider circuit breakers (shared with orchestrator).
_rules_engine_singleton: RulesEngine | None = None
_openai_breaker: CircuitBreaker = load_openai_circuit_breaker()
_gemini_breaker: CircuitBreaker = load_gemini_circuit_breaker()


def _build_openai_model() -> ChatOpenAI:
    """
    Build the OpenAI fallback client for rules-flow subagents (tool calling).

    Returns:
        ChatOpenAI: Configured OpenAI client.

    Raises:
        RuntimeError: If ``OPENAI_API_KEY_QUTIS`` is not set.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY_QUTIS is required for rules-flow subagents")

    return ChatOpenAI(
        model=OPENAI_MODEL_NAME,
        api_key=OPENAI_API_KEY,
        timeout=OPENAI_REQUEST_TIMEOUT_S,
        max_retries=0,
        temperature=0,
        max_tokens=ORCHESTRATOR_MAX_TOKENS,
    )


def _build_gemini_model() -> Any:
    """
    Build the Gemini primary client for rules-flow subagents.

    Returns:
        Any: Configured Gemini chat model.
    """
    return build_gemini_chat_model(
        model=GEMINI_MODEL_NAME,
        temperature=0,
        max_tokens=ORCHESTRATOR_MAX_TOKENS,
        thinking_level=GEMINI_THINKING_LEVEL,
        google_credentials_path=GOOGLE_CREDENTIALS_PATH,
        google_project=GOOGLE_PROJECT_ID,
        google_location=GOOGLE_LOCATION,
        gemini_api_key=GEMINI_API_KEY,
    )


def _should_failover_to_openai(exc: Exception) -> bool:
    """
    Return True when Gemini failure should trigger OpenAI fallback.

    Args:
        exc: Raised exception from the primary provider.

    Returns:
        bool: True when OpenAI should be attempted next.
    """
    return _should_retry_provider_error(exc)


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


def _invoke_agent_once(
    *,
    agent: Any,
    prompt: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> dict[str, Any]:
    """
    Invoke an agent once and normalize the response shape.

    Args:
        agent: Agent instance.
        prompt: User prompt.
        invoke_config: Optional LangSmith/checkpointer invoke config.

    Returns:
        dict[str, Any]: Agent invoke response.
    """
    if invoke_config is not None:
        response = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=invoke_config,
        )
    else:
        response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    return response if isinstance(response, dict) else {}


def _invoke_with_retry(
    *,
    primary_agent: Any,
    fallback_agent: Any,
    prompt: str,
    operation: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> dict[str, Any]:
    """
    Invoke Gemini first, then OpenAI on rate limits or circuit-open state.

    Args:
        primary_agent: Gemini-backed agent.
        fallback_agent: OpenAI-backed fallback agent.
        prompt: The prompt for the agent.
        operation: The operation carried out (skin or product review research).

    Returns:
        dict[str, Any]: Agent invoke response.

    Raises:
        RuntimeError: When both providers fail or circuits are open.
    """
    if not _gemini_breaker.allow():
        logger.warning(
            "gemini circuit open (state=%s); using openai for %s",
            _gemini_breaker.state,
            operation,
        )
        return _invoke_openai_once(
            agent=fallback_agent,
            prompt=prompt,
            operation=operation,
            invoke_config=invoke_config,
        )

    try:
        response = _invoke_agent_once(
            agent=primary_agent,
            prompt=prompt,
            invoke_config=invoke_config,
        )
        _gemini_breaker.record_success()
        return response
    except Exception as exc:
        _gemini_breaker.record_failure()
        if not _should_failover_to_openai(exc):
            raise
        logger.warning(
            "%s gemini failed (%s); failing over to openai",
            operation,
            exc,
        )
        return _invoke_openai_once(
            agent=fallback_agent,
            prompt=prompt,
            operation=operation,
            invoke_config=invoke_config,
        )


def _invoke_openai_once(
    *,
    agent: Any,
    prompt: str,
    operation: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> dict[str, Any]:
    """
    Invoke the OpenAI fallback agent with circuit-breaker guarding.

    Args:
        agent: OpenAI-backed agent.
        prompt: User prompt.
        operation: Operation name for logging/errors.

    Returns:
        dict[str, Any]: Agent invoke response.

    Raises:
        RuntimeError: When the OpenAI circuit is open or the call fails.
    """
    if not _openai_breaker.allow():
        raise RuntimeError(
            f"openai circuit open (state={_openai_breaker.state}); skipping {operation}"
        )
    try:
        response = _invoke_agent_once(
            agent=agent,
            prompt=prompt,
            invoke_config=invoke_config,
        )
        _openai_breaker.record_success()
        return response
    except Exception as exc:
        _openai_breaker.record_failure()
        raise


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


def _limit_sources(sources: list[str], limit: int = _REPORT_MAX_SOURCES) -> list[str]:
    """
    Return up to ``limit`` unique source URLs for outbound messages.

    Args:
        sources: Candidate source URLs.
        limit: Maximum number of sources to retain.

    Returns:
        list[str]: Deduplicated sources, capped at ``limit``.
    """
    unique: list[str] = []
    for item in sources:
        candidate = str(item).strip()
        if not candidate or candidate in unique:
            continue
        unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique


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
def skin_review_search(query: str, max_results: int = 2) -> str:
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


def _build_ingredient_agents() -> tuple[Any, Any]:
    """
    Build Gemini-primary and OpenAI-fallback ingredient extraction agents.

    Returns:
        tuple[Any, Any]: Primary and fallback agent instances.
    """
    system_prompt = (
        "You extract cosmetic product ingredient names in strict INCI-style naming. "
        "Always use the inci_decoder_search tool before finalizing when product identity is available. "
        "Return only ingredient names without concentration or percentages. "
        "Example: 'Niacinamide 10%' -> 'Niacinamide'. "
        "Never return explanatory prose in ingredients; keep it clean and deduplicated."
        "Return at-most the first 10 main ingredients."
        "Avoid formatting your responses, reply with a clean well structured response, No bold or italics."
    )
    primary = create_agent(
        model=_build_gemini_model(),
        tools=[inci_decoder_search],
        response_format=IngredientExtractionResponse,
        system_prompt=system_prompt,
    )
    fallback = create_agent(
        model=_build_openai_model(),
        tools=[inci_decoder_search],
        response_format=IngredientExtractionResponse,
        system_prompt=system_prompt,
    )
    return primary, fallback


def extract_product_ingredients(
    *,
    product_name: str,
    description: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> IngredientExtractionResponse:
    """
    Extract product ingredients.
    Args:
        product_name (str): The name of the product
        description (str): The description of the product
    
    Returns:
        IngredientExtractionResponse: The extracted ingredients
    """
    primary_agent, fallback_agent = _build_ingredient_agents()
    prompt = (
        "Extract the ingredient list for this cosmetic product.\n"
        "Use INCI names only and remove concentration values.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
    )
    try:
        response = _invoke_with_retry(
            primary_agent=primary_agent,
            fallback_agent=fallback_agent,
            prompt=prompt,
            operation="ingredient_extraction",
            invoke_config=invoke_config,
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
def report_internet_search(query: str, max_results: int = 2) -> str:
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

def _build_report_agents(
    context: list[dict[str, Any]],
    delivery_prompt: str = "",
) -> tuple[Any, Any]:
    """
    Build Gemini-primary and OpenAI-fallback product report agents.

    Returns:
        tuple[Any, Any]: Primary and fallback agent instances.
    """
    system_prompt = (
        "You are a dermatology-focused product-audit assistant. "
        "Convert structured product safety evidence into a short, readable audit for a non-technical user. "
        "Do not claim medical diagnosis and do not prescribe treatment. "
        "Use report_internet_search only when it meaningfully strengthens unclear or missing safety points. "
        "Target 100-180 words total. Use 1-2 short sentences per section; no bullet laundry lists. "
        "Do NOT report ingredient-by-ingredient. Synthesize across the formula into safety themes "
        "(e.g. pregnancy/lactation cautions, contraindications or drug interactions, irritation or "
        "sensitization risk, photosensitivity, actives to introduce slowly). "
        "For a retinol serum, emphasize pregnancy avoidance, irritation potential, and sun protection "
        "rather than listing every INCI name. "
        "Output must use these section labels in order (plain text, no markdown): "
        "Overview; Key safety themes; Contraindications and interaction cautions; "
        "Pregnancy and lactation considerations; What to do next; Safety disclaimer. "
        "Overview must include the product name when known. "
        "Safety disclaimer must state the review is educational, not medical advice, and not a substitute "
        "for a healthcare professional. "
        "Do not include URLs in the report body (sources are appended separately). "
        "When client context includes pregnancy, breastfeeding, allergies, or goals, "
        "address them explicitly in pregnancy/lactation and safety sections. "
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        f"{delivery_prompt}\n"
    )
    primary = create_agent(
        model=_build_gemini_model(),
        tools=[report_internet_search],
        response_format=UserFriendlyReportResponse,
        system_prompt=system_prompt,
    )
    fallback = create_agent(
        model=_build_openai_model(),
        tools=[report_internet_search],
        response_format=UserFriendlyReportResponse,
        system_prompt=system_prompt,
    )
    return primary, fallback


def _build_skin_review_agents(
    context: list[dict[str, Any]],
    delivery_prompt: str = "",
) -> tuple[Any, Any]:
    """
    Build Gemini-primary and OpenAI-fallback skin review agents.

    Returns:
        tuple[Any, Any]: Primary and fallback agent instances.
    """
    system_prompt = (
        "You are a dermatology skin-audit assistant. "
        "Produce a short image-based skin pattern review from the candidate pattern, condition summary, "
        "and needs/treatment context. "
        "Use skin_review_search only when it materially improves uncertain or thin evidence. "
        "Target 80-150 words total. Use 1-2 short sentences per section. "
        "Return plain text only; do not use markdown, bold, or italics. "
        "Do not prescribe medications, dosages, or definitive treatment plans. "
        "Treat the candidate pattern as an internal hypothesis, not a confirmed diagnosis. "
        "Use cautious language such as 'appears consistent with', 'may suggest', 'could reflect', "
        "and 'image-based review cannot confirm'. "
        "Avoid definitive phrasing such as 'has', 'is', 'diagnosis is', or 'confirms'. "
        "If evidence is limited, state uncertainty explicitly. "
        "Use these section labels in order: "
        "Observed pattern; What this may suggest; Care considerations (general, non-prescriptive); "
        "When to seek in-person review; Safety disclaimer. "
        "Safety disclaimer must state the review is educational, not a medical diagnosis or prescription, "
        "and that image-based review cannot confirm a condition. "
        "Do not include URLs in the report body (sources are appended separately). "
        "Personalize care considerations using allergies, pregnancy/breastfeeding status, "
        "skin type, and the user's primary concern when provided in client context. "
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        f"{delivery_prompt}\n"
    )
    primary = create_agent(
        model=_build_gemini_model(),
        tools=[skin_review_search],
        response_format=SkinReviewResponse,
        system_prompt=system_prompt,
    )
    fallback = create_agent(
        model=_build_openai_model(),
        tools=[skin_review_search],
        response_format=SkinReviewResponse,
        system_prompt=system_prompt,
    )
    return primary, fallback


def build_skin_review_report(
    *,
    diagnosis: str,
    summary: str,
    treatment: str,
    internet_research: str,
    context: list[dict[str, Any]],
    delivery: Optional[DeliveryContext] = None,
    invoke_config: Optional[RunnableConfig] = None,
) -> str:
    """Build a concise user-facing skin review report."""
    delivery_prompt = format_delivery_context_for_prompt(delivery) if delivery else ""
    primary_agent, fallback_agent = _build_skin_review_agents(context, delivery_prompt)
    prompt = (
        "Generate the final image-based skin pattern review using the inputs below.\n"
        "Keep every required section label; be concise (1-2 sentences per section).\n"
        "If a field is missing, state 'unknown' briefly and continue.\n"
        "Personalize using client context when allergies, pregnancy, or goals are present.\n"
        f"candidate_pattern: {diagnosis}\n"
        f"skin_condition_summary: {summary}\n"
        f"needs_and_treatment: {treatment}\n"
        f"internet_research_on_candidate_pattern: {internet_research}\n"
    )
    if delivery_prompt:
        prompt = f"{prompt}\n{delivery_prompt}\n"
    try:
        response = _invoke_with_retry(
            primary_agent=primary_agent,
            fallback_agent=fallback_agent,
            prompt=prompt,
            operation="skin_review_report",
            invoke_config=invoke_config,
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
    delivery: Optional[DeliveryContext] = None,
    invoke_config: Optional[RunnableConfig] = None,
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
    primary_agent, fallback_agent = _build_report_agents(
        context,
        format_delivery_context_for_prompt(delivery) if delivery else "",
    )
    limited_sources = _limit_sources(sources)
    prompt = (
        "Generate the final product audit using the inputs below.\n"
        "Synthesize safety themes across ingredients; do not list each ingredient separately.\n"
        "Include the product name in Overview when available.\n"
        "If a field is missing, state 'unknown' briefly and continue.\n"
        "Personalize pregnancy/lactation and allergy guidance using client context when present.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
        f"ingredients: {json.dumps(ingredients, default=str)}\n"
        f"rules_engine_report_json: {json.dumps(report, default=str)}\n"
        f"reference_sources: {json.dumps(limited_sources, default=str)}\n"
    )
    try:
        response = _invoke_with_retry(
            primary_agent=primary_agent,
            fallback_agent=fallback_agent,
            prompt=prompt,
            operation="product_user_friendly_report",
            invoke_config=invoke_config,
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
    signal_confidence = extract_confidence_from_fields(fields)
    signal_threshold = extract_threshold_from_fields(fields)

    trace_config = build_invoke_config(
        thread_id=client_id,
        checkpoint_ns="signal_rules:product",
        run_name="signal_rules:product",
        channel=channel,
        flow="signal",
        client_id=client_id,
        request_id=request_id,
        case_id=case_id,
        message_id=message_id,
        extra_metadata={
            "review_type": "product",
            **({"confidence": signal_confidence} if signal_confidence else {}),
            **({"threshold": signal_threshold} if signal_threshold else {}),
        },
        extra_tags=["product"],
    )

    delivery = load_delivery_context(case_id, client_id)
    skin_context = SkinContext.model_validate(build_rules_skin_context(delivery))

    product_name = str(payload.get("product_name") or "").strip()
    description = str(payload.get("description") or "").strip()

    latest_skin_audit, _ = _load_latest_skin_audit(client_id)
    latest_skin_audit = merge_profile_into_skin_audit(latest_skin_audit, delivery)

    logger.info(
        "product signal rules flow started: request_id=%s case_id=%s client_id=%s product_name=%s",
        request_id,
        case_id,
        client_id,
        product_name or "<empty>",
    )

    extraction = extract_product_ingredients(
        product_name=product_name,
        description=description,
        invoke_config=trace_config,
    )
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
        skin_context=skin_context,
        escalation_notes=delivery.collected_context_summary or delivery.primary_concern,
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
        delivery=delivery,
        invoke_config=trace_config,
    )
    outbound_sources = _limit_sources(result.sources)

    return {
        "summary": result.summary,
        "user_friendly_report": user_friendly_report,
        "sources": outbound_sources,
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
    signal_confidence = extract_confidence_from_fields(fields)
    signal_threshold = extract_threshold_from_fields(fields)

    trace_config = build_invoke_config(
        thread_id=client_id,
        checkpoint_ns="signal_rules:skin",
        run_name="signal_rules:skin",
        channel=channel,
        flow="signal",
        client_id=client_id,
        request_id=request_id,
        case_id=case_id,
        message_id=message_id,
        extra_metadata={
            "review_type": "skin",
            **({"confidence": signal_confidence} if signal_confidence else {}),
            **({"threshold": signal_threshold} if signal_threshold else {}),
        },
        extra_tags=["skin"],
    )

    delivery = load_delivery_context(case_id, client_id)
    skin_context = SkinContext.model_validate(build_rules_skin_context(delivery))

    diagnosis = str(payload.get("diagnosis") or payload.get("diagnosis_text") or "").strip()
    summary = _coerce_summary_payload(payload.get("summary"))
    treatment = _coerce_treatment_payload(payload.get("treatment"))
    diagnosis_normalized = diagnosis.lower()

    logger.info(
        "skin signal rules flow started: request_id=%s case_id=%s client_id=%s diagnosis=%s",
        request_id,
        case_id,
        client_id,
        diagnosis or "<empty>",
    )

    if diagnosis_normalized == "uncertain":
        logger.info(
            "skin signal marked uncertain; skipping rules engine: request_id=%s case_id=%s client_id=%s",
            request_id,
            case_id,
            client_id,
        )
        user_friendly_report = (
            "Thanks for sharing the image. The current image-based review does not provide enough clear visual evidence "
            "for a reliable skin pattern assessment. Please share a clearer, well-lit close-up image, or seek an "
            "in-person dermatology review if symptoms are worsening or concerning. This information is educational "
            "and not a medical diagnosis or prescription."
        )
        return {
            "summary": "Image-based skin review is uncertain due to limited visual evidence.",
            "user_friendly_report": user_friendly_report,
            "sources": [],
            "report": {
                "diagnosis": "uncertain",
                "summary": summary,
                "treatment": treatment,
            },
        }

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
        skin_context=skin_context,
        escalation_notes=delivery.collected_context_summary or delivery.primary_concern,
        raw_model_json=str(fields.get("payload") or ""),
    )

    logger.info("skin rules engine request created: request_id=%s", request_id)
    result = rules_engine.process(request)

    report = result.report if isinstance(result.report, dict) else {}
    user_friendly_report = build_skin_review_report(
        diagnosis=str(report.get("diagnosis") or diagnosis),
        summary=str(report.get("summary") or summary),
        treatment=str(report.get("treatment") or treatment),
        internet_research=str(report.get("internet_research") or ""),
        context=context,
        delivery=delivery,
        invoke_config=trace_config,
    )

    return {
        "summary": result.summary,
        "user_friendly_report": user_friendly_report,
        "sources": _limit_sources(result.sources),
        "report": result.report,
    }

if __name__ == "__main__":

    res = extract_product_ingredients(product_name="La Roche-Posay Anthelios Oil Control Fluid Uvmune 400 SPF50 Sunscreen+ 50ml", description="Anti-ageing")
    print(res)
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
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.tools import tool
from pydantic import BaseModel, Field
from tavily import TavilyClient

from dermascan.backend.services.agents.model_fallback_chain import (
    AGENT_INVOKE_MAX_RETRIES,
    AGENT_INVOKE_RETRY_DELAY_S,
    MODEL_CHAIN_MAX_RETRIES,
    MODEL_CHAIN_RETRY_DELAY_S,
    ModelChainRunner,
    build_model_chain_from_spec,
    report_model_chain_spec,
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
OPENAI_API_KEY = str(os.getenv("OPENAI_API_KEY_QUTIS", ""))
OPENAI_REQUEST_TIMEOUT_S = float(os.getenv("OPENAI_REQUEST_TIMEOUT_S", "45"))
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
_FINAL_REPORT_MAX_SENTENCES = max(3, int(os.getenv("SIGNAL_RULES_MAX_SENTENCES", "5")))
REPORT_AGENT_RECURSION_LIMIT: int = max(
    4, int(os.getenv("REPORT_AGENT_RECURSION_LIMIT", "8"))
)
REPORT_AGENT_INVOKE_MAX_RETRIES: int = max(
    0,
    int(os.getenv("REPORT_AGENT_INVOKE_MAX_RETRIES", str(AGENT_INVOKE_MAX_RETRIES))),
)
REPORT_AGENT_INVOKE_RETRY_DELAY_S: float = max(
    0.0,
    float(os.getenv("REPORT_AGENT_INVOKE_RETRY_DELAY_S", str(AGENT_INVOKE_RETRY_DELAY_S))),
)
REPORT_AGENT_TOOL_RUN_LIMIT: int = max(
    2, int(os.getenv("REPORT_AGENT_TOOL_RUN_LIMIT", "6"))
)
REPORT_AGENT_INCI_SEARCH_RUN_LIMIT: int = max(
    1, int(os.getenv("REPORT_AGENT_INCI_SEARCH_RUN_LIMIT", "2"))
)
REPORT_AGENT_INTERNET_SEARCH_RUN_LIMIT: int = max(
    1, int(os.getenv("REPORT_AGENT_INTERNET_SEARCH_RUN_LIMIT", "2"))
)

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

# Global rules engine instance.
_rules_engine_singleton: RulesEngine | None = None
_report_chain_runner: ModelChainRunner | None = None


def _get_report_chain_runner() -> ModelChainRunner:
    """
    Return the shared report/ingredient model chain runner.

    Returns:
        ModelChainRunner: Cached runner with per-model circuit breakers.
    """
    global _report_chain_runner
    if _report_chain_runner is None:
        model_chain = build_model_chain_from_spec(
            chain_spec=report_model_chain_spec(),
            openai_api_key=OPENAI_API_KEY,
            openai_temperature=0.0,
            openai_timeout_s=OPENAI_REQUEST_TIMEOUT_S,
            gemini_temperature=0.0,
            gemini_max_tokens=ORCHESTRATOR_MAX_TOKENS,
            gemini_top_p=0.95,
            gemini_thinking_level=GEMINI_THINKING_LEVEL,
            google_credentials_path=GOOGLE_CREDENTIALS_PATH,
            google_project=GOOGLE_PROJECT_ID,
            google_location=GOOGLE_LOCATION,
            gemini_api_key=GEMINI_API_KEY,
        )
        _report_chain_runner = ModelChainRunner(
            model_chain,
            max_retries=MODEL_CHAIN_MAX_RETRIES,
            retry_delay_s=MODEL_CHAIN_RETRY_DELAY_S,
            agent_max_retries=REPORT_AGENT_INVOKE_MAX_RETRIES,
            agent_retry_delay_s=REPORT_AGENT_INVOKE_RETRY_DELAY_S,
        )
        logger.info(
            "report model chain: %s (chain_retries=%s, retry_delay_s=%s, "
            "agent_retries=%s, agent_retry_delay_s=%s, recursion_limit=%s)",
            " -> ".join(name for name, _ in model_chain),
            MODEL_CHAIN_MAX_RETRIES,
            MODEL_CHAIN_RETRY_DELAY_S,
            REPORT_AGENT_INVOKE_MAX_RETRIES,
            REPORT_AGENT_INVOKE_RETRY_DELAY_S,
            REPORT_AGENT_RECURSION_LIMIT,
        )
    return _report_chain_runner


def _tool_names(tools: list[Any]) -> set[str]:
    """
    Extract registered tool names from LangChain tool objects.

    Args:
        tools: Tool list passed to a report subagent.

    Returns:
        set[str]: Tool name slugs.
    """
    names: set[str] = set()
    for tool_obj in tools:
        name = getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", "")
        if name:
            names.add(str(name))
    return names


def _build_report_agent_middleware(tools: list[Any]) -> list[Any]:
    """
    Build per-tool and global tool-call limits for report subagents.

    Args:
        tools: Tools attached to the subagent.

    Returns:
        list[Any]: Middleware stack for ``create_agent``.
    """
    names = _tool_names(tools)
    middleware: list[Any] = []
    if "inci_decoder_search" in names:
        middleware.append(
            ToolCallLimitMiddleware(
                tool_name="inci_decoder_search",
                run_limit=REPORT_AGENT_INCI_SEARCH_RUN_LIMIT,
                exit_behavior="continue",
            )
        )
    if "report_internet_search" in names:
        middleware.append(
            ToolCallLimitMiddleware(
                tool_name="report_internet_search",
                run_limit=REPORT_AGENT_INTERNET_SEARCH_RUN_LIMIT,
                exit_behavior="continue",
            )
        )
    if "skin_review_search" in names:
        middleware.append(
            ToolCallLimitMiddleware(
                tool_name="skin_review_search",
                run_limit=REPORT_AGENT_INTERNET_SEARCH_RUN_LIMIT,
                exit_behavior="continue",
            )
        )
    middleware.append(
        ToolCallLimitMiddleware(
            run_limit=REPORT_AGENT_TOOL_RUN_LIMIT,
            exit_behavior="continue",
        )
    )
    return middleware


def _merge_invoke_config(
    invoke_config: Optional[RunnableConfig],
    *,
    recursion_limit: int,
) -> RunnableConfig:
    """
    Merge recursion limit into an invoke config for subagent calls.

    Args:
        invoke_config: Optional base RunnableConfig (e.g. LangSmith trace).
        recursion_limit: LangGraph step cap for this invoke.

    Returns:
        RunnableConfig: Config with recursion_limit applied.
    """
    merged: dict[str, Any] = dict(invoke_config) if invoke_config else {}
    merged["recursion_limit"] = recursion_limit
    return merged


def _invoke_with_model_chain(
    *,
    system_prompt: str,
    tools: list[Any],
    response_format: type[BaseModel],
    prompt: str,
    operation: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> dict[str, Any]:
    """
    Invoke a LangChain agent across the ordered model chain.

    Args:
        system_prompt: Agent system prompt.
        tools: Tool list for the agent.
        response_format: Structured output schema.
        prompt: User prompt.
        operation: Operation label for logs.
        invoke_config: Optional RunnableConfig.

    Returns:
        dict[str, Any]: Agent invoke response.
    """
    runner = _get_report_chain_runner()
    config = _merge_invoke_config(
        invoke_config,
        recursion_limit=REPORT_AGENT_RECURSION_LIMIT,
    )
    middleware = _build_report_agent_middleware(tools)

    def build_agent(model: Any) -> Any:
        return create_agent(
            model=model,
            tools=tools,
            response_format=response_format,
            system_prompt=system_prompt,
            middleware=middleware,
        )

    return runner.invoke_langchain_model(
        prompt=prompt,
        build_agent=build_agent,
        invoke_config=config,
        operation=operation,
    )


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


def _enforce_max_sentences(text: str, max_sentences: int = _FINAL_REPORT_MAX_SENTENCES) -> str:
    """
    Trim a report to at most ``max_sentences`` complete sentences.

    Args:
        text: Report body to trim.
        max_sentences: Maximum number of sentences to keep.

    Returns:
        str: Trimmed report text.
    """
    cleaned = _strip_markdown_formatting(text)
    if not cleaned:
        return cleaned

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(sentences) <= max_sentences:
        return cleaned
    return " ".join(sentences[:max_sentences])


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


def _ingredient_extraction_system_prompt() -> str:
    """
    Build the system prompt for ingredient extraction agents.

    Returns:
        str: System prompt text.
    """
    return (
        "You extract cosmetic product ingredient names in strict INCI-style naming. "
        "Always use the inci_decoder_search tool before finalizing when product identity is available. "
        "Return only ingredient names without concentration or percentages. "
        "Example: 'Niacinamide 10%' -> 'Niacinamide'. "
        "Never return explanatory prose in ingredients; keep it clean and deduplicated."
        "Return at-most the first 10 main ingredients."
        "Avoid formatting your responses, reply with a clean well structured response, No bold or italics."
    )


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
    system_prompt = _ingredient_extraction_system_prompt()
    prompt = (
        "Extract the ingredient list for this cosmetic product.\n"
        "Use INCI names only and remove concentration values.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
    )
    try:
        response = _invoke_with_model_chain(
            system_prompt=system_prompt,
            tools=[inci_decoder_search],
            response_format=IngredientExtractionResponse,
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

def _product_report_system_prompt(
    context: list[dict[str, Any]],
    delivery_prompt: str = "",
) -> str:
    """
    Build the system prompt for product report agents.

    Args:
        context: Recent conversation entries for personalization.
        delivery_prompt: Optional delivery-channel formatting hints.

    Returns:
        str: System prompt text.
    """
    return (
        "You are a dermatology-focused product-audit assistant. "
        "Convert structured product safety evidence into a brief audit for a non-technical user. "
        "Do not claim medical diagnosis and do not prescribe treatment. "
        "Use report_internet_search only when it meaningfully strengthens unclear or missing safety points. "
        "Write at most 5 sentences total as one plain-text paragraph. "
        "Do NOT use section labels, headings, bullet lists, or markdown. "
        "Prioritize findings that matter for THIS user (pregnancy, breastfeeding, allergies, goals, "
        "primary concern). Lead with the highest-risk mismatch first — e.g. if the user is pregnant "
        "and the formula contains retinol, say that clearly in sentence 1. "
        "Do NOT mention filler or low-risk ingredients (water, aqua, glycerin, basic emollients, "
        "common preservatives) unless they are the main safety issue. "
        "Synthesize across the formula into a few safety themes only (contraindications, pregnancy/lactation, "
        "irritation/sensitization, photosensitivity, actives to introduce slowly). "
        "Include the product name in sentence 1 when known. "
        "End with one short educational disclaimer: not medical advice; consult a professional when unsure. "
        "Do not include URLs in the report body (sources are appended separately). "
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        f"{delivery_prompt}\n"
    )


def _skin_review_system_prompt(
    context: list[dict[str, Any]],
    delivery_prompt: str = "",
) -> str:
    """
    Build the system prompt for skin review report agents.

    Args:
        context: Recent conversation entries for personalization.
        delivery_prompt: Optional delivery-channel formatting hints.

    Returns:
        str: System prompt text.
    """
    return (
        "You are a dermatology skin-audit assistant. "
        "Produce a brief image-based skin pattern review from the candidate pattern, condition summary, "
        "and needs/treatment context. "
        "Use skin_review_search only when it materially improves uncertain or thin evidence. "
        "Write at most 5 sentences total as one plain-text paragraph. "
        "Do NOT use section labels, headings, bullet lists, or markdown. "
        "Do not prescribe medications, dosages, or definitive treatment plans. "
        "Treat the candidate pattern as an internal hypothesis, not a confirmed diagnosis. "
        "Use cautious language such as 'appears consistent with', 'may suggest', 'could reflect', "
        "and 'image-based review cannot confirm'. "
        "Avoid definitive phrasing such as 'has', 'is', 'diagnosis is', or 'confirms'. "
        "If evidence is limited, state uncertainty explicitly. "
        "Suggested flow (still one paragraph, no labels): what the image may show; what that could mean; "
        "one practical care consideration personalized to allergies, pregnancy/breastfeeding, skin type, "
        "and primary concern when provided; when to seek in-person review; one short educational disclaimer "
        "(not a diagnosis or prescription; image review cannot confirm a condition). "
        "Do not include URLs in the report body (sources are appended separately). "
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        f"{delivery_prompt}\n"
    )


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
    system_prompt = _skin_review_system_prompt(context, delivery_prompt)
    prompt = (
        "Generate the final image-based skin pattern review using the inputs below.\n"
        f"Hard limit: at most {_FINAL_REPORT_MAX_SENTENCES} sentences in one paragraph; no section labels.\n"
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
        response = _invoke_with_model_chain(
            system_prompt=system_prompt,
            tools=[skin_review_search],
            response_format=SkinReviewResponse,
            prompt=prompt,
            operation="skin_review_report",
            invoke_config=invoke_config,
        )
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, SkinReviewResponse) and structured.report_text.strip():
            return _enforce_max_sentences(structured.report_text)
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
    delivery_prompt = format_delivery_context_for_prompt(delivery) if delivery else ""
    system_prompt = _product_report_system_prompt(context, delivery_prompt)
    limited_sources = _limit_sources(sources)
    prompt = (
        "Generate the final product audit using the inputs below.\n"
        f"Hard limit: at most {_FINAL_REPORT_MAX_SENTENCES} sentences in one paragraph; no section labels.\n"
        "Rank findings by patient relevance: contraindications that match the user's stated pregnancy, "
        "breastfeeding, allergies, or goals come first; skip filler ingredients unless they are the main risk.\n"
        "Include the product name in sentence 1 when available.\n"
        "If a field is missing, state 'unknown' briefly and continue.\n"
        "Personalize pregnancy/lactation and allergy guidance using client context when present.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
        f"ingredients: {json.dumps(ingredients, default=str)}\n"
        f"rules_engine_report_json: {json.dumps(report, default=str)}\n"
        f"reference_sources: {json.dumps(limited_sources, default=str)}\n"
    )
    try:
        response = _invoke_with_model_chain(
            system_prompt=system_prompt,
            tools=[report_internet_search],
            response_format=UserFriendlyReportResponse,
            prompt=prompt,
            operation="product_user_friendly_report",
            invoke_config=invoke_config,
        )
        structured = response.get("structured_response") if isinstance(response, dict) else None
        if isinstance(structured, UserFriendlyReportResponse) and structured.report_text.strip():
            return _enforce_max_sentences(structured.report_text)
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
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

from dermascan.ai_service.postprocessing.medgemma_structured_output import resolve_skin_observation_text
from dermascan.backend.services.agents.model_fallback_chain import (
    AGENT_INVOKE_MAX_RETRIES,
    AGENT_INVOKE_RETRY_DELAY_S,
    MODEL_CHAIN_MAX_RETRIES,
    MODEL_CHAIN_RETRY_DELAY_S,
    ModelChainRunner,
    build_model_chain_from_spec,
    report_model_chain_spec,
    serial_tool_calls_middleware,
)
from dermascan.rules_engine.confidence.decision_confidence import (
    GuidanceMode,
    finalize_product_report_text,
    product_report_guidance_mode_instructions,
)
from dermascan.rules_engine.engine.engine import RulesEngineRequest, SkinAudit, SkinContext
from dermascan.rules_engine.engine.rules_engine import RulesEngine
from dermascan.shared.cosmetic_feature_flags import cosmetic_guidance_mode_active
from dermascan.shared.cosmetic_profile import (
    is_skin_snapshot_fresh,
    resolve_product_personalization_mode,
)
from dermascan.shared.cosmetic_types import (
    GENERIC_PRODUCT_REPORT_DISCLAIMER,
    PROFILE_ONLY_PRODUCT_REPORT_DISCLAIMER,
    PersonalizationMode,
    normalize_personalization_mode,
    product_report_personalization_guidance,
)
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
from dermascan.shared.skin_audit_store import (
    load_latest_skin_audit,
    parse_signal_confidence,
    persist_skin_audit_snapshot,
)
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
# Shown to the user when no ingredients could be read for a product review.
PRODUCT_LABEL_UNREADABLE_MESSAGE: str = os.getenv(
    "PRODUCT_LABEL_UNREADABLE_MESSAGE",
    "I couldn't read the ingredient list for this product. Please resend a clear, "
    "well-lit close-up of the ingredients label (text in focus and fully visible) so "
    "the specialist team can review it. This is informational only, not medical advice.",
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
    middleware.append(serial_tool_calls_middleware)
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


def _load_latest_skin_audit(client_id: str) -> tuple[dict[str, Any], int | None]:
    """Load latest skin audit snapshot from Redis (delegates to shared store)."""
    return load_latest_skin_audit(client_id)


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
    Build the system prompt for the ingredient structuring model call.

    Returns:
        str: System prompt text.
    """
    return (
        "You extract cosmetic product ingredient names in strict INCI-style naming "
        "from the provided reference text. "
        "Use ONLY the supplied product name and search reference text; do not invent "
        "ingredients or call any tools. "
        "Return only ingredient names without concentration or percentages. "
        "Example: 'Niacinamide 10%' -> 'Niacinamide'. "
        "Never return explanatory prose in ingredients; keep it clean and deduplicated. "
        "Return at most the first 10 main ingredients in order. "
        "If the reference text does not contain a usable ingredient list, return an empty "
        "ingredients array and briefly say why in notes. "
        "Avoid formatting; no bold or italics."
    )


def _search_incidecoder_reference(product_name: str, *, max_results: int = 5) -> str:
    """
    Run a single deterministic INCI Decoder search for a product.

    This is Phase A of ingredient extraction: a controlled, non-agent tool call so
    no model decides when to stop searching (removes the ReAct recursion loop).

    Args:
        product_name: Product identity to search for.
        max_results: Maximum search results to request.

    Returns:
        str: Raw search text (JSON string) or an empty string when unavailable.

    Time complexity: O(1) network calls.
    """
    query = f"{product_name} ingredients".strip()
    if not query or query == "ingredients":
        return ""
    try:
        return str(
            inci_decoder_search.invoke(
                {"query": query, "max_results": max_results}
            )
        )
    except Exception:
        logger.exception("inci_decoder_search failed during phase-A lookup")
        return ""


def extract_product_ingredients(
    *,
    product_name: str,
    description: str,
    invoke_config: Optional[RunnableConfig] = None,
) -> IngredientExtractionResponse:
    """
    Extract product ingredients using a two-phase, recursion-proof pipeline.

    Phase A performs a single deterministic ``inci_decoder_search`` (no agent, no
    model-driven loop). Phase B makes one structured-output model call with NO tools
    bound, so a ReAct graph is never created and ``GraphRecursionError`` cannot occur.

    Args:
        product_name: The name of the product.
        description: Optional description / supplemental text.
        invoke_config: Optional RunnableConfig for tracing.

    Returns:
        IngredientExtractionResponse: Extracted ingredients (empty on failure).
    """
    # Phase A: deterministic reference lookup (bounded, cannot loop).
    reference_text = _search_incidecoder_reference(product_name)

    # Phase B: single structured-output call, no tools -> recursion impossible.
    system_prompt = _ingredient_extraction_system_prompt()
    prompt = (
        "Extract the INCI ingredient list for this cosmetic product from the "
        "reference text below. Remove concentrations. Return at most 10 main "
        "ingredients in order.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
        f"search_reference_text: {reference_text or '(no reference text found)'}\n"
    )
    try:
        runner = _get_report_chain_runner()
        structured = runner.invoke_structured_output(
            messages=[("system", system_prompt), ("human", prompt)],
            schema=IngredientExtractionResponse,
            config=invoke_config,
            operation="ingredient_extraction",
        )
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
    *,
    personalization_mode: PersonalizationMode = "personalized",
    guidance_mode: GuidanceMode | None = None,
    apply_guidance_mode: bool = False,
) -> str:
    """
    Build the system prompt for product report agents.

    Args:
        context: Recent conversation entries for personalization.
        delivery_prompt: Optional delivery-channel formatting hints.
        personalization_mode: How strongly the report may reference skin/profile context.
        guidance_mode: Decision-confidence guidance band when Step 7.1 is active.
        apply_guidance_mode: When True, include guidance-mode instructions in the prompt.

    Returns:
        str: System prompt text.
    """
    mode_guidance = product_report_personalization_guidance(personalization_mode)
    guidance_line = ""
    if apply_guidance_mode and guidance_mode is not None:
        guidance_line = product_report_guidance_mode_instructions(guidance_mode)
    personalization_clause = (
        "Prioritize findings that matter for THIS user (pregnancy, breastfeeding, allergies, goals, "
        "primary concern). Lead with the highest-risk mismatch first — e.g. if the user is pregnant "
        "and the formula contains retinol, say that clearly in sentence 1. "
    )
    if personalization_mode == "generic":
        personalization_clause = (
            "Focus on formula-wide safety themes (contraindications, pregnancy/lactation when "
            "universally relevant to the ingredient, irritation/sensitization, photosensitivity, "
            "actives to introduce slowly). Do not rank findings against a specific user profile. "
        )
    elif personalization_mode == "profile_only":
        personalization_clause = (
            "Prioritize findings that match the user's STATED profile (pregnancy, breastfeeding, "
            "allergies, goals, primary concern). Lead with the highest-risk mismatch first. "
            "Do not reference image-based or vision-derived skin observations. "
        )

    return (
        "You are a cosmetic skincare product-safety assistant. "
        "Convert structured product safety evidence into a brief audit for a non-technical user. "
        "Do not claim medical diagnosis and do not prescribe treatment. "
        "Use report_internet_search only when it meaningfully strengthens unclear or missing safety points. "
        "Compress the report into at most 5 short sentences in total, use bullet points if needed. "
        "Do NOT use markdown formatting. "
        f"{personalization_clause}"
        "Do NOT mention filler or low-risk ingredients (water, aqua, glycerin, basic emollients, "
        "common preservatives) unless they are the main safety issue. "
        "Synthesize across the formula into a few safety themes only (contraindications, pregnancy/lactation, "
        "irritation/sensitization, photosensitivity, actives to introduce slowly). "
        "Include the product name in sentence 1 when known. "
        "End with one short educational disclaimer: not medical advice; consult a professional when unsure. "
        "Do not include URLs in the report body (sources are appended separately). "
        f"{mode_guidance}\n"
        f"{guidance_line}\n"
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
        f"{delivery_prompt}\n"
    )


def _product_report_user_prompt(
    *,
    product_name: str,
    description: str,
    ingredients: list[str],
    report: dict[str, Any],
    limited_sources: list[str],
    personalization_mode: PersonalizationMode,
    guidance_mode: GuidanceMode | None = None,
    apply_guidance_mode: bool = False,
) -> str:
    """
    Build the user prompt for the product report agent.

    Args:
        product_name: Product name from label read or user input.
        description: Product description text.
        ingredients: Normalized INCI ingredient list.
        report: Rules engine structured report.
        limited_sources: Capped reference source list.
        personalization_mode: Active personalization mode.
        guidance_mode: Decision-confidence guidance band when Step 7.1 is active.
        apply_guidance_mode: When True, include guidance-mode instructions in the prompt.

    Returns:
        str: User prompt text.
    """
    mode_line = product_report_personalization_guidance(personalization_mode)
    guidance_line = ""
    if apply_guidance_mode and guidance_mode is not None:
        guidance_line = product_report_guidance_mode_instructions(guidance_mode)
    return (
        "Generate the final product audit using the inputs below.\n"
        f"Hard limit: at most {_FINAL_REPORT_MAX_SENTENCES} sentences in one paragraph; no section labels.\n"
        f"{mode_line}\n"
        f"{guidance_line}\n"
        "Include the product name in sentence 1 when available.\n"
        "If a field is missing, state 'unknown' briefly and continue.\n"
        f"product_name: {product_name}\n"
        f"description: {description}\n"
        f"ingredients: {json.dumps(ingredients, default=str)}\n"
        f"rules_engine_report_json: {json.dumps(report, default=str)}\n"
        f"reference_sources: {json.dumps(limited_sources, default=str)}\n"
    )


def _product_report_fallback_message(
    mode: PersonalizationMode,
    *,
    guidance_mode: GuidanceMode | None = None,
    apply_guidance_mode: bool = False,
) -> str:
    """
    Return a mode-appropriate fallback when report generation fails.

    Args:
        mode: Active personalization mode.
        guidance_mode: Decision-confidence guidance band when Step 7.1 is active.
        apply_guidance_mode: When True, append guidance-mode footers.

    Returns:
        str: Short fallback message for the user.
    """
    if mode == "generic":
        base = (
            "General product safety review completed. "
            f"{GENERIC_PRODUCT_REPORT_DISCLAIMER} "
            "Please follow product caution guidance and consult a professional when unsure."
        )
    elif mode == "profile_only":
        base = (
            "Product safety review completed based on your stated profile. "
            f"{PROFILE_ONLY_PRODUCT_REPORT_DISCLAIMER} "
            "Please follow product caution guidance and consult a professional when unsure."
        )
    else:
        base = (
            "Safety review completed. Please consult the detailed report and follow product "
            "caution guidance."
        )
    return finalize_product_report_text(
        base,
        personalization_mode=mode,
        guidance_mode=guidance_mode,
        apply_guidance_mode=apply_guidance_mode,
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
        "Compress the report into at most 5 short sentences in total, use bullet points if needed. "
        "Do NOT use markdown formatting. "
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
    personalization_mode: PersonalizationMode = "personalized",
    guidance_mode: GuidanceMode | None = None,
    apply_guidance_mode: bool = False,
) -> str:
    """
    Build a user-friendly report from the rules engine report.
    
    Args:
        product_name: Name of the product
        description: Product description
        ingredients: List of ingredients
        report: Rules engine report
        sources: List of sources
        context: Recent conversation entries for personalization.
        delivery: Optional delivery context for prompt formatting.
        invoke_config: Optional RunnableConfig for tracing.
        personalization_mode: How strongly the report may reference skin/profile context.
        guidance_mode: Decision-confidence guidance band from the rules engine.
        apply_guidance_mode: When True, shape prompts and footers by guidance mode.

    Returns:
        User-friendly report as string
    """
    mode = normalize_personalization_mode(personalization_mode)
    delivery_prompt = format_delivery_context_for_prompt(delivery) if delivery else ""
    system_prompt = _product_report_system_prompt(
        context,
        delivery_prompt,
        personalization_mode=mode,
        guidance_mode=guidance_mode,
        apply_guidance_mode=apply_guidance_mode,
    )
    limited_sources = _limit_sources(sources)
    prompt = _product_report_user_prompt(
        product_name=product_name,
        description=description,
        ingredients=ingredients,
        report=report,
        limited_sources=limited_sources,
        personalization_mode=mode,
        guidance_mode=guidance_mode,
        apply_guidance_mode=apply_guidance_mode,
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
            report_text = _enforce_max_sentences(structured.report_text)
            return finalize_product_report_text(
                report_text,
                personalization_mode=mode,
                guidance_mode=guidance_mode,
                apply_guidance_mode=apply_guidance_mode,
            )
    except Exception:
        logger.exception("failed to build user-friendly product report")

    return _product_report_fallback_message(
        mode,
        guidance_mode=guidance_mode,
        apply_guidance_mode=apply_guidance_mode,
    )


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

    delivery = load_delivery_context(case_id, client_id)
    skin_context = SkinContext.model_validate(build_rules_skin_context(delivery))

    product_name = str(payload.get("product_name") or "").strip()
    description = str(payload.get("description") or "").strip()

    latest_skin_audit, skin_audit_updated_at_ts = _load_latest_skin_audit(client_id)
    latest_skin_audit = merge_profile_into_skin_audit(latest_skin_audit, delivery)

    snapshot_fresh = is_skin_snapshot_fresh(latest_skin_audit, skin_audit_updated_at_ts)

    personalization_mode: PersonalizationMode = resolve_product_personalization_mode(
        skin_audit=latest_skin_audit,
        profile=delivery.skin_profile,
        intake_session=delivery.intake_session,
        snapshot_fresh=snapshot_fresh,
    )

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
            "personalization_mode": personalization_mode,
            **({"confidence": signal_confidence} if signal_confidence else {}),
            **({"threshold": signal_threshold} if signal_threshold else {}),
        },
        extra_tags=["product"],
    )

    logger.info(
        "product signal rules flow started: request_id=%s case_id=%s client_id=%s "
        "product_name=%s personalization_mode=%s text_only=%s",
        request_id,
        case_id,
        client_id,
        product_name or "<empty>",
        personalization_mode,
        bool(payload.get("text_only")),
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

    # No readable ingredients: skip the rules engine (it would run on an empty list
    # and produce a misleading "no info" report) and ask for a clearer label photo.
    if not extraction.ingredients:
        logger.warning(
            "product review has no readable ingredients; skipping rules engine: "
            "request_id=%s case_id=%s client_id=%s product_name=%s",
            request_id,
            case_id,
            client_id,
            product_name or "<empty>",
        )
        return {
            "summary": "",
            "user_friendly_report": PRODUCT_LABEL_UNREADABLE_MESSAGE,
            "sources": [],
            "ingredients": [],
            "indexed_ingredients": [],
            "report": {},
            "ingredients_unreadable": True,
        }

    rules_engine = get_rules_engine()
    skin_audit_model = SkinAudit.model_validate(latest_skin_audit)
    request = RulesEngineRequest(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        message_id=message_id,
        channel=channel,
        review_type="product",
        personalization_mode=personalization_mode,
        ingredients=extraction.ingredients,
        skin_audit=skin_audit_model,
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

    logger.info(
        "rules engine request created: request_id=%s personalization_mode=%s has_skin_context=%s",
        request_id,
        request.personalization_mode,
        request.has_skin_context(),
    )

    # Run rules engine process with the built request
    result = rules_engine.process(request)
    
    logger.info(
        "rules engine completed: request_id=%s indexed_ingredients=%s "
        "decision_confidence=%s confidence_tier=%s guidance_mode=%s",
        request_id,
        len(result.indexed_ingredients),
        result.decision_confidence,
        result.confidence_tier,
        result.guidance_mode,
    )
    if result.confidence_components:
        logger.info(
            "decision confidence components: request_id=%s components=%s",
            request_id,
            result.confidence_components,
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
        personalization_mode=personalization_mode,
        guidance_mode=result.guidance_mode,
        apply_guidance_mode=cosmetic_guidance_mode_active(),
    )
    outbound_sources = _limit_sources(result.sources)

    return {
        "summary": result.summary,
        "user_friendly_report": user_friendly_report,
        "sources": outbound_sources,
        "ingredients": extraction.ingredients,
        "indexed_ingredients": [item.model_dump() for item in result.indexed_ingredients],
        "report": result.report,
        "personalization_mode": personalization_mode,
        "decision_confidence": result.decision_confidence,
        "confidence_tier": result.confidence_tier,
        "guidance_mode": result.guidance_mode,
        "confidence_components": dict(result.confidence_components),
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
    primary_observation = str(payload.get("primary_observation") or "").strip()
    observation = resolve_skin_observation_text(payload) or diagnosis
    summary = _coerce_summary_payload(payload.get("skin_state") or payload.get("summary"))
    treatment = _coerce_treatment_payload(
        payload.get("cosmetic_implications") or payload.get("treatment")
    )
    observation_normalized = observation.lower()

    logger.info(
        "skin signal rules flow started: request_id=%s case_id=%s client_id=%s observation=%s",
        request_id,
        case_id,
        client_id,
        observation or "<empty>",
    )

    if observation_normalized == "uncertain":
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
    vision_confidence = parse_signal_confidence(signal_confidence)
    request = RulesEngineRequest(
        request_id=request_id,
        case_id=case_id,
        client_id=client_id,
        message_id=message_id,
        channel=channel,
        review_type="skin",
        diagnosis=diagnosis or observation,
        primary_observation=primary_observation or observation,
        risk_tier=str(payload.get("risk_tier") or "cosmetic"),
        image_quality=str(payload.get("image_quality") or "unknown"),
        summary=summary,
        treatment=treatment,
        skin_context=skin_context,
        skin_audit=SkinAudit(confidence=vision_confidence),
        escalation_notes=delivery.collected_context_summary or delivery.primary_concern,
        raw_model_json=json.dumps(payload, default=str),
    )

    logger.info("skin rules engine request created: request_id=%s", request_id)
    result = rules_engine.process(request)

    report = result.report if isinstance(result.report, dict) else {}
    skin_audit_payload = report.get("skin_audit")
    if isinstance(skin_audit_payload, dict):
        persist_skin_audit_snapshot(
            client_id,
            skin_audit=skin_audit_payload,
            request_id=request_id,
            primary_observation=primary_observation or observation,
            risk_tier=str(payload.get("risk_tier") or "cosmetic"),
            image_quality=str(payload.get("image_quality") or "unknown"),
            signal_confidence=signal_confidence,
        )

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
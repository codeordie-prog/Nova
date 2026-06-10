"""

Helpers for product-review signal enrichment via ingredient extraction + rules engine."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from dermascan.rules_engine.engine.engine import RulesEngineRequest
from dermascan.rules_engine.engine.rules_engine import RulesEngine
from dermascan.shared.logger import get_logger

load_dotenv()

logger = get_logger(__name__)

TAVILY_API_KEY = str(os.getenv("TAVILY_API_KEY_QUTIS", ""))
OPENAI_API_KEY = str(os.getenv("OPENAI_API_KEY_QUTIS", ""))
PRODUCT_INGREDIENT_MODEL = str(os.getenv("PRODUCT_INGREDIENT_MODEL", "gpt-4o-mini"))
PRODUCT_REPORT_MODEL = str(os.getenv("PRODUCT_REPORT_MODEL", "gpt-4o-mini"))
SKIN_REVIEW_MODEL = str(os.getenv("SKIN_REVIEW_MODEL", "gpt-4o-mini"))


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

# Global rules engine instance
_rules_engine_singleton: RulesEngine | None = None


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
def skin_review_search(query: str, max_results: int = 8) -> str:
    """
    Search for dermatology and skin condition information from reputable medical sources.

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
    model = ChatOpenAI(
        model=PRODUCT_INGREDIENT_MODEL, 
        api_key=OPENAI_API_KEY, 
        temperature=0
    )
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
    response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    structured = response.get("structured_response") if isinstance(response, dict) else None
    if isinstance(structured, IngredientExtractionResponse):
        ingredients = [_normalize_inci_name(item) for item in structured.ingredients]
        ingredients = [item for item in ingredients if item]
        return IngredientExtractionResponse(
            ingredients=list(dict.fromkeys(ingredients)),
            notes=structured.notes,
            sources=structured.sources,
        )

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
    model = ChatOpenAI(
        model=PRODUCT_REPORT_MODEL, 
        api_key=OPENAI_API_KEY, 
        temperature=0
    )
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
    model = ChatOpenAI(
        model=SKIN_REVIEW_MODEL,
        api_key=OPENAI_API_KEY,
        temperature=0
    )

    return create_agent(
        model=model,
        tools=[skin_review_search],
        response_format=SkinReviewResponse,
        system_prompt=(
            "You are a dermatology research assistant. "
            "Research the provided skin condition/diagnosis to provide evidence-based information. "
            "Focus on: common causes, typical symptoms, and general treatment approaches. "
            "Always avoid definitive treatment plans - provide information only, not prescriptions. "
            "Use the skin_review_search tool to gather information from reputable medical sources. "
            "Include disclaimers that this is for informational purposes and not medical advice. "
            "Do not prescribe medications or claim definitive diagnosis. "
            "Use the last_conversation_entries to provide context for the report."
            "When reviewing the skin condition, the last_conversation_entries will help you structure your response to the user's query."
            f"last_conversation_entries: {json.dumps(context, default=str)}\n"
            "Return plain text only; do not use markdown, bold, or italics."
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
        "Create a user-friendly skin research report.\n"
        "Include: likely/common causes, common treatment approaches, and practical caution notes.\n"
        "Avoid definitive treatment plans or direct prescriptions.\n"
        "Use the last_conversation_entries to provide context for the report.\n"
        "Return plain text only; do not use markdown, bold, or italics.\n"
        f"diagnosis: {diagnosis}\n"
        f"summary: {summary}\n"
        f"treatment: {treatment}\n"
        f"internet_research: {internet_research}\n"
        f"last_conversation_entries: {json.dumps(context, default=str)}\n"
    )
    try:
        response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
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
        response = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
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
    summary = str(payload.get("summary") or "").strip()
    treatment = str(payload.get("treatment") or "").strip()

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
"""
Author: Dermascan Team
Date: 2026-5-10
Licence: MIT
Description: This module implements the tools for the knowledge agent.

"""

from datetime import datetime, timezone
import os

from dotenv import load_dotenv
from langchain.tools import tool
from tavily import TavilyClient

load_dotenv()

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")


def _get_tavily_client() -> TavilyClient | None:
    if not TAVILY_API_KEY:
        return None
    return TavilyClient(api_key=TAVILY_API_KEY)


@tool
def wikipedia_search(query: str, max_results: int = 5, *args, **kwargs) -> str:
    """
    Search Wikipedia content through Tavily.

    Use this tool when you need background explanations, definitions, and
    high-level context. Do not rely on this tool alone for critical medical
    safety claims; cross-check key claims with broader web sources.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
        *args: Ignored extra positional arguments.
        **kwargs: Ignored extra keyword arguments.
    """
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    try:
        result = client.search(
            query=query,
            max_results=max(1, min(max_results, 8)),
            include_domains=["wikipedia.org"],
        )
        return str(result)
    except Exception as exc:
        return f"Wikipedia search failed: {exc}"


@tool
def internet_search(query: str, max_results: int = 8, *args, **kwargs) -> str:
    """
    Perform general web research through Tavily.

    Use this as the primary research tool for up-to-date and credible external
    sources (guidelines, journals, hospital/public-health pages, and trusted
    references). Prefer this tool for safety checks and practical guidance.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
        *args: Ignored extra positional arguments.
        **kwargs: Ignored extra keyword arguments.
    """
    client = _get_tavily_client()
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    try:
        result = client.search(
            query=query,
            max_results=max(1, min(max_results, 10)),
            search_depth="advanced",
            include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "mydawa.com"],
        )
        return str(result)
    except Exception as exc:
        return f"Internet search failed: {exc}"


@tool
def get_date_time(*args, **kwargs) -> str:
    """
    Get the current UTC date-time in ISO-8601 format.

    Use this tool when time context matters (e.g., "as of today" language,
    freshness checks, or timeline-sensitive guidance).

    Args:
        *args: Ignored extra positional arguments.
        **kwargs: Ignored extra keyword arguments.
    """
    return datetime.now(timezone.utc).isoformat()


knowledge_tools: list = [
    wikipedia_search,
    internet_search,
    get_date_time
]
"""
Author: Dermascan Team
Date: 2026-05-21
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: Knowledge agent implementation

The Agent core purpose is to analyze, supplement and summarize the supplied Toxicological/Safety reports
for Cosmetic ingredients.
"""

import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from tavily import TavilyClient

from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain.tools import tool
from langchain.agents.middleware import SummarizationMiddleware, wrap_model_call, ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model


from dermascan.rules_engine.knowledge_agent.prompt import system_prompt

# Load environment variables
load_dotenv()

OPENAI_API_KEY: str = str(os.environ.get("OPENAI_API_KEY_QUTIS", ""))
TAVILY_API_KEY: str = str(os.environ.get("TAVILY_API_KEY_QUTIS", ""))

class ResponseFormat(BaseModel):
    summary: str = Field(description="The report summary of the provided ingredients.")
    sources: list[str] = Field(default=["https://cir-reports.cir-safety.org/"],description="List of the sources")


def _get_tavily_client() -> TavilyClient | None:
    """
    Create Tavily client instance.
    
    Returns:
        TavilyClient instance if API key is available, None otherwise
    """
    if not TAVILY_API_KEY:
        return None
    return TavilyClient(api_key=TAVILY_API_KEY)

@tool
def internet_search(query: str, max_results: int = 8) -> str:
    """
    Perform general web research through Tavily.

    Use this as the primary research tool for up-to-date and credible external
    sources (guidelines, journals, hospital/public-health pages, and trusted
    references). Prefer this tool for safety checks and practical guidance.

    Args:
        query: Search query string.
        max_results: Maximum number of results to retrieve (capped internally).
    """
    client: TavilyClient = _get_tavily_client()
    
    if client is None:
        return "Tavily is not configured. Set TAVILY_API_KEY to enable internet research."

    else:
        try:
            result = client.search(
                query=query,
                max_results=max(1, min(max_results, 10)),
                search_depth="advanced",
                include_domains=["mayoclinic.org", "webmd.com", "healthline.com", "cdc.gov", "nih.gov", "fda.gov", "pubchem.ncbi.nlm.nih.gov"],
            )
            #return str(result)
            import json
            return json.dumps(result, default=str)
        except Exception as exc:
            return f"Internet search failed: {exc}"


def init_model_with_tools(api_key: str = OPENAI_API_KEY):
    """
    Initialize OpenAI model

    Args:
        api_key (str): OPENAI API KEY.

    
    """
    model =  init_chat_model(
        "gpt-4o-mini",
        api_key=api_key,
    )

    return model.bind_tools([internet_search])


def create_knowledge_agent(model, system_prompt: str, api_key: str = OPENAI_API_KEY):
    """
    Create a knowledge agent with the given model and system prompt.
    
    Args:
        model: The model to use for the agent.
        system_prompt: The system prompt to use for the agent.
        api_key: The API key to use for the agent.
    """
    model = init_model_with_tools(api_key=api_key)

    return create_agent(
        model=model, 
        system_prompt=system_prompt,
        tools=[internet_search]
    )


if __name__ == "__main__":
    model = init_model_with_tools()
    structured_model = model.with_structured_output(ResponseFormat)
    agent = create_knowledge_agent(
        model=structured_model,
        system_prompt=system_prompt,
    )
    res = agent.invoke({"messages": [{"role": "user", "content": "Retinol serum ingredient concentrations"}]})
    final_msg = next(
        m for m in reversed(res["messages"])
        if hasattr(m, "type") and m.type == "ai" and not (hasattr(m, "tool_calls") and m.tool_calls)
    )

    print(final_msg.content)


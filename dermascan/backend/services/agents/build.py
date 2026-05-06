from typing import Any, Optional, Type

from google.oauth2 import service_account
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from dermascan.backend.services.agents.memory.redis_checkpointer import setup_from_url


def build_checkpointer_url(url: str) -> Any:
    """
    Build a Checkpointer instance from a Redis URL.

    Args:
        url: Redis URL

    Returns:
        Checkpointer instance
    """
    if not url:
        raise RuntimeError("REDIS_URL is required for Redis checkpointing")
    saver, _ = setup_from_url(url)
    return saver


def open_checkpointer_url(url: str) -> tuple[Any, Any]:
    """
    Open a Redis-backed checkpointer and return (checkpointer, context_manager).

    Caller must close the returned context manager to release resources.
    """
    if not url:
        raise RuntimeError("REDIS_URL is required for Redis checkpointing")
    return setup_from_url(url)


def build_google_credentials(path: str) -> service_account.Credentials:
    """
    Google credentials from the specified path to service account credentials.

    Args:
        path: Path to the service account credentials file
    
    Returns:
        Google credentials
    """
    if not path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS_QUTIS is required for Google provider")
    return service_account.Credentials.from_service_account_file(
        path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def build_agent(
    model: Any,
    tools: list,
    checkpointer: Any,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
) -> Any:
    """
    Build an agent with the specified model, tools, and checkpointer.
    
    Args:
        model: The model to use for the agent
        tools: List of tools to make available to the agent
        checkpointer: Checkpointer instance for state management
        system_prompt: Optional system prompt to guide the agent's behavior
        response_format: Optional response format to enforce structured output
    
    Returns:
        Configured agent instance
    """
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=[
            SummarizationMiddleware(
                model=model,
                trigger=("tokens", 1000),
                keep=("messages", 10),
            )
        ],
        checkpointer=checkpointer,
        response_format=response_format,
    )


def initialize_openai_agent(
    cfg: dict[str, Any],
    tools: list,
    checkpointer: Any,
    openai_api_key: str,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
) -> Any:
    """Initialize an OpenAI-backed agent."""
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI provider")

    model = ChatOpenAI(
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        verbose=True,
        api_key=openai_api_key,
    )
    return build_agent(
        model=model,
        tools=tools,
        checkpointer=checkpointer,
        system_prompt=system_prompt,
        response_format=response_format,
    )


def initialize_google_agent(
    cfg: dict[str, Any],
    tools: list,
    checkpointer: Any,
    google_credentials_path: str,
    google_project: str,
    google_location: str,
    gemini_api_key: str,
    system_prompt: Optional[str] = None,
    response_format: Optional[Type[Any]] = None,
) -> Any:
    """Initialize a Google-backed agent."""
    creds = build_google_credentials(path=google_credentials_path)
    model = ChatGoogleGenerativeAI(
        model=cfg["model"],
        credentials=creds,
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        top_p=cfg["top_p"],
        verbose=True,
        location=google_location,
        project=google_project,
        api_key=gemini_api_key,
    )
    return build_agent(
        model=model,
        tools=tools,
        checkpointer=checkpointer,
        system_prompt=system_prompt,
        response_format=response_format,
    )

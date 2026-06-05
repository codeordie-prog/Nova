"""
Author: DermaScan Team
Date: 2026-06-04
Description: Minimal LangChain agent script using Qwen + Tavily + datetime tools.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI
import modal
from tavily import TavilyClient


load_dotenv()


def _get_tavily_client() -> TavilyClient:
    """
    Create a Tavily client from environment configuration.

    Returns:
        TavilyClient: Initialized Tavily client.

    Raises:
        RuntimeError: If the Tavily API key is missing.
    """
    api_key: str = (os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_API_KEY_QUTIS") or "").strip()
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY (or TAVILY_API_KEY_QUTIS) is required")
    return TavilyClient(api_key=api_key)


@tool("current_datetime_utc")
def current_datetime_utc() -> str:
    """
    Return current UTC date-time in ISO-8601 format.

    Returns:
        str: UTC timestamp (ISO-8601, seconds precision, with trailing Z).
    """
    now: datetime = datetime.now(UTC).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


@tool("tavily_search")
def tavily_search(query: str) -> str:
    """
    Search the web using Tavily and return compact JSON results.

    Args:
        query: Search query text.

    Returns:
        str: JSON string with top search results.

    Raises:
        RuntimeError: If Tavily returns an unexpected payload.
    """
    client: TavilyClient = _get_tavily_client()
    response: dict[str, Any] = client.search(
        query=query,
        max_results=5,
        include_answer=True,
        include_raw_content=False,
    )

    if not isinstance(response, dict):
        raise RuntimeError("Unexpected Tavily response type")

    payload: dict[str, Any] = {
        "query": query,
        "answer": response.get("answer"),
        "results": response.get("results", []),
    }
    return json.dumps(payload, ensure_ascii=True)


def _build_model() -> ChatOpenAI:
    """
    Build ChatOpenAI client targeting a Qwen OpenAI-compatible endpoint.

    Returns:
        ChatOpenAI: Configured chat model client.
    """
    base_url: str = os.getenv("QWEN_OPENAI_BASE_URL", "http://127.0.0.1:8001/v1").strip()
    model_name: str = os.getenv("QWEN_MODEL_NAME", "Qwen/Qwen3-4B").strip()
    api_key: str = os.getenv("QWEN_OPENAI_API_KEY", "EMPTY").strip() or "EMPTY"

    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        temperature=float(os.getenv("QWEN_TEMPERATURE", "0.2")),
        max_tokens=int(os.getenv("QWEN_MAX_TOKENS", "800")),
    )


def run_agent(prompt: str) -> str:
    """
    Run one agent turn with Tavily and datetime tools.

    Args:
        prompt: User input prompt.

    Returns:
        str: Final assistant response text.
    """
    model: ChatOpenAI = _build_model()
    agent = create_agent(
        model=model,
        tools=[tavily_search, current_datetime_utc],
        system_prompt=(
            "You are a concise assistant. Use tools when web facts or current time are needed. "
            "Always cite search findings briefly."
        ),
    )

    result: dict[str, Any] = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
    messages: list[Any] = list(result.get("messages", []))
    if not messages:
        return ""

    final_message: Any = messages[-1]
    content: Any = getattr(final_message, "content", "")
    return str(content)


def run_modal_qwen_test(query: str) -> dict[str, Any]:
    """
    Run a direct Modal RPC smoke test against the Qwen service.

    This path invokes ``QwenService.run_one.remote`` so Modal spins up the
    service container (if needed) and runs one model request end-to-end.

    Args:
        query: User query to send to Qwen.

    Returns:
        dict[str, Any]: Structured test output including boot ID and response.

    Raises:
        RuntimeError: If the Modal response does not contain model content.
    """
    from dermascan.ai_service.modal.qwen_service.app import QwenService, app as qwen_app

    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]

    # This remote call triggers Modal container startup when cold.
    with modal.enable_output():
        with qwen_app.run():
            boot_id, result = QwenService().run_one.remote(
                messages=messages,
                system_prompt="You are a concise helpful assistant.",
            )

    content: Any = result.get("content")
    if content is None or str(content).strip() == "":
        raise RuntimeError(f"Modal Qwen test returned empty content: {result}")

    return {
        "ok": True,
        "mode": "modal_smoke_test",
        "boot_id": boot_id,
        "content": str(content),
        "finish_reason": result.get("finish_reason"),
        "tool_calls": result.get("tool_calls", []),
        "usage": result.get("usage", {}),
    }


def run_modal_qwen_tool_test(query: str) -> dict[str, Any]:
    """
    Run a direct Modal RPC test that requires Qwen to call Tavily search.

    Args:
        query: User query crafted to trigger Tavily function usage.

    Returns:
        dict[str, Any]: Test result with tool-calling details.

    Raises:
        RuntimeError: If Tavily tool-calling does not occur or is invalid.
    """
    from dermascan.ai_service.modal.qwen_service.app import QwenService, app as qwen_app

    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "tavily_search",
                "description": "Search the web and return top relevant findings.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The web search query text.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    with modal.enable_output():
        with qwen_app.run():
            boot_id, result = QwenService().run_one.remote(
                messages=messages,
                system_prompt=(
                    "You are a strict tool-using assistant. "
                    "Do not answer directly before calling the provided function."
                ),
                tools=tools,
                tool_choice="required",
                temperature=0.0,
                extra={"chat_template_kwargs": {"enable_thinking": False}},
            )

    tool_calls: list[dict[str, Any]] = list(result.get("tool_calls", []))
    if not tool_calls:
        raise RuntimeError(f"Tool test failed: model returned no tool_calls. Raw result={result}")
    first_call: dict[str, Any] = tool_calls[0]
    function_obj: dict[str, Any] = dict(first_call.get("function", {}))
    tool_name: str = str(function_obj.get("name", ""))
    if tool_name != "tavily_search":
        raise RuntimeError(f"Tool test failed: expected tavily_search, got {tool_name}. Raw={result}")

    arguments_raw: Any = function_obj.get("arguments", "{}")
    arguments: dict[str, Any]
    if isinstance(arguments_raw, str):
        arguments = json.loads(arguments_raw)
    elif isinstance(arguments_raw, dict):
        arguments = arguments_raw
    else:
        raise RuntimeError(f"Tool test failed: invalid arguments format: {arguments_raw!r}")

    search_query: str = str(arguments.get("query", "")).strip()
    if not search_query:
        raise RuntimeError(f"Tool test failed: tavily_search query missing. Raw={result}")

    # Execute the real Tavily tool locally, then send tool result back to model.
    tool_output: str = tavily_search.invoke({"query": search_query})
    tool_call_id: str = str(first_call.get("id", "call_1"))
    followup_messages: list[dict[str, Any]] = [
        {"role": "user", "content": query},
        {"role": "assistant", "content": result.get("content") or "", "tool_calls": tool_calls},
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": "tavily_search",
            "content": tool_output,
        },
    ]

    with modal.enable_output():
        with qwen_app.run():
            _, final_result = QwenService().run_one.remote(
                messages=followup_messages,
                system_prompt="Use tool results to provide a concise factual answer.",
                tools=tools,
                tool_choice="none",
                temperature=0.0,
            )

    final_content: str = str(final_result.get("content") or "").strip()
    if not final_content:
        raise RuntimeError(f"Tool test failed: final answer content is empty. Raw={final_result}")

    return {
        "ok": True,
        "mode": "modal_tavily_tool_test",
        "boot_id": boot_id,
        "tavily_query": search_query,
        "tool_calls": tool_calls,
        "tool_output": tool_output,
        "content": final_content,
        "finish_reason": final_result.get("finish_reason"),
        "usage": final_result.get("usage", {}),
    }


def _parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed arguments object.
    """
    parser = argparse.ArgumentParser(description="Run a simple Qwen LangChain agent with tools.")
    parser.add_argument(
        "prompt",
        type=str,
        nargs="?",
        default="What time is it now in UTC and what are two top AI headlines today?",
        help="Prompt to send to the agent.",
    )
    modal_group = parser.add_mutually_exclusive_group()
    modal_group.add_argument(
        "--modal-test",
        action="store_true",
        help="Run direct Modal RPC test against QwenService instead of LangChain path.",
    )
    modal_group.add_argument(
        "--modal-tool-test",
        action="store_true",
        help="Run direct Modal RPC test that validates model tool-calling output.",
    )
    return parser.parse_args()


def main() -> None:
    """
    CLI entrypoint.

    Raises:
        RuntimeError: If required environment variables are missing.
    """
    args: argparse.Namespace = _parse_args()
    if args.modal_tool_test:
        tool_test_prompt: str = (
            args.prompt
            or "Find two recent AI headlines from trusted sources and summarize them."
        )
        modal_result: dict[str, Any] = run_modal_qwen_tool_test(tool_test_prompt)
        print(json.dumps(modal_result, ensure_ascii=True, indent=2))
        return

    if args.modal_test:
        modal_result: dict[str, Any] = run_modal_qwen_test(args.prompt)
        print(json.dumps(modal_result, ensure_ascii=True, indent=2))
        return

    output: str = run_agent(args.prompt)
    print(output)


if __name__ == "__main__":
    main()

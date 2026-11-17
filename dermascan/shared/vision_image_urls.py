"""
Author: Dermascan Team
Date: 2026-08-16
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Refresh ephemeral Firebase signed URLs in multimodal LLM message content.

Durable image identity is the ``client-media/...`` blob path (see escalation flow).
Signed URLs expire after ``WORKERS_JOB_TTL_S``; LangGraph checkpoints replay them.
This module re-signs client-media references immediately before vision model calls."""


from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from dermascan.shared.firebase_storage import normalize_image_blob_path, sign_client_image_blob_path
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

_UNAVAILABLE_IMAGE_TEXT: str = "[prior image unavailable — storage reference could not be refreshed]"
CHECKPOINT_IMAGE_BLOCK_TYPE: str = "image_blob"


def build_checkpoint_image_block(blob_path: str) -> dict[str, str]:
    """
    Build a durable multimodal block for LangGraph checkpoint persistence.

    Checkpoints must not store signed HTTPS URLs. The vision middleware converts
    ``image_blob`` blocks to fresh ``image_url`` blocks immediately before each
    model call.

    Args:
        blob_path: Durable ``client-media/...`` Firebase object path.

    Returns:
        OpenAI-style content block dict with type ``image_blob``.

    Raises:
        ValueError: When ``blob_path`` is empty or outside ``client-media/``.
    """
    normalized = normalize_image_blob_path(str(blob_path or "").strip())
    if not normalized:
        raise ValueError("blob_path must resolve to a client-media object path")
    return {
        "type": CHECKPOINT_IMAGE_BLOCK_TYPE,
        "blob_path": normalized,
    }


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    """
    Walk an exception and its ``__cause__`` / ``__context__`` chain.

    Args:
        exc: Root exception.

    Returns:
        Ordered list of exceptions from outermost to innermost.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def is_invalid_image_url_error(exc: Exception) -> bool:
    """
    Detect vision failures caused by expired or unreachable image URLs.

    Args:
        exc: Raised exception from a model invocation.

    Returns:
        bool: True when the provider rejected an ``image_url`` block.
    """
    markers: tuple[str, ...] = (
        "invalid_image_url",
        "invalid image url",
        "error while downloading",
    )
    for current in _iter_exception_chain(exc):
        text: str = str(current).lower()
        if any(marker in text for marker in markers):
            return True
    return False


def resolve_client_media_blob_path(reference: str) -> str:
    """
    Normalize a client-media blob path or Firebase/GCS HTTPS URL.

    Args:
        reference: Durable path or signed URL.

    Returns:
        ``client-media/...`` path, or empty string when not a client-media reference.
    """
    return normalize_image_blob_path(str(reference or "").strip())


def refresh_vision_image_url(url: str) -> str:
    """
    Return a fresh signed URL for client-media references.

    Non-client-media URLs (``data:``, external hosts) are returned unchanged.

    Args:
        url: Vision-compatible image URL or blob path.

    Returns:
        Fresh signed HTTPS URL, original URL, or empty string when refresh failed.
    """
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    if normalized.lower().startswith("data:"):
        return normalized

    blob_path = resolve_client_media_blob_path(normalized)
    if not blob_path:
        return normalized

    try:
        return sign_client_image_blob_path(blob_path)
    except Exception:
        logger.warning(
            "failed to refresh vision image URL: blob_path=%s",
            blob_path,
            exc_info=True,
        )
        return ""


def _refresh_content_block(block: Any) -> Any:
    """
    Convert checkpoint-safe image blocks into signed vision URLs.

    Supports durable ``image_blob`` blocks (Phase C) and legacy ``image_url``
    blocks that carry Firebase signed URLs or bare blob paths.

    Args:
        block: OpenAI-style content block dict.

    Returns:
        ``image_url`` block, original block, or a text placeholder on failure.
    """
    if not isinstance(block, dict):
        return block

    block_type = str(block.get("type") or "").strip().lower()
    if block_type == CHECKPOINT_IMAGE_BLOCK_TYPE:
        blob_path = str(block.get("blob_path") or "").strip()
        refreshed_url = refresh_vision_image_url(blob_path)
        if not refreshed_url:
            return {"type": "text", "text": _UNAVAILABLE_IMAGE_TEXT}
        return {
            "type": "image_url",
            "image_url": {"url": refreshed_url},
        }

    if block_type != "image_url":
        return block

    image_url_obj = block.get("image_url")
    if not isinstance(image_url_obj, dict):
        return {"type": "text", "text": _UNAVAILABLE_IMAGE_TEXT}

    original_url = str(image_url_obj.get("url") or "").strip()
    if not original_url:
        return {"type": "text", "text": _UNAVAILABLE_IMAGE_TEXT}

    refreshed_url = refresh_vision_image_url(original_url)
    if not refreshed_url:
        return {"type": "text", "text": _UNAVAILABLE_IMAGE_TEXT}

    if refreshed_url == original_url:
        return block

    return {
        "type": "image_url",
        "image_url": {"url": refreshed_url},
    }


def refresh_message_vision_urls(message: BaseMessage) -> BaseMessage:
    """
    Refresh expired client-media URLs in one LangChain message.

    Args:
        message: Conversation message about to be sent to a vision model.

    Returns:
        Original message or a copy with refreshed multimodal content blocks.
    """
    content = message.content
    if not isinstance(content, list):
        return message

    refreshed_blocks = [_refresh_content_block(block) for block in content]
    if refreshed_blocks == content:
        return message

    return message.model_copy(update={"content": refreshed_blocks}, deep=True)


def refresh_messages_vision_urls(messages: list[Any]) -> list[Any]:
    """
    Refresh client-media signed URLs across a message list.

    Args:
        messages: Messages bound for a model request.

    Returns:
        List with refreshed multimodal HumanMessage content where applicable.
    """
    refreshed: list[Any] = []
    changed = False
    for message in messages:
        if isinstance(message, HumanMessage):
            updated = refresh_message_vision_urls(message)
            refreshed.append(updated)
            changed = changed or updated is not message
        else:
            refreshed.append(message)
    return refreshed if changed else messages

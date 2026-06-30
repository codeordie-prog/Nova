"""
Author: Dermascan Team
Date: 2026-06-28
Licence: MIT

Ingress payload helpers: image attachment detection and URL resolution for
orchestrator multimodal input and MedGemma enqueue.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Callable, Final, Optional

from pydantic import BaseModel, Field

_LOGGER: Final[logging.Logger] = logging.getLogger("dermascan.ingress_payload")


class ResolvedIngressImage(BaseModel):
    """
    One image attachment resolved for model or audit consumption.

    Attributes:
        attachment_id: Attachment identifier from ingress payload.
        image_url: HTTPS URL or data-URL suitable for vision APIs.
        mime_type: Image MIME type when known.
        index: Zero-based index among image attachments in the payload.
    """

    attachment_id: str = ""
    image_url: str = Field(min_length=1)
    mime_type: str = ""
    index: int = 0


def is_image_attachment(attachment: Any) -> bool:
    """
    Return True when an attachment dict represents an image.

    Args:
        attachment: Attachment object from ingress payload.

    Returns:
        bool: True for image attachments.
    """
    if not isinstance(attachment, dict):
        return False
    attachment_type = str(attachment.get("type") or "")
    mime_type = str(attachment.get("mime_type") or "")
    return attachment_type == "image" or mime_type.startswith("image/")


def _redis_get(redis_client: Any, key: str) -> str:
    """
    Read a string value from Redis, returning empty string on failure.

    Args:
        redis_client: Redis client instance.
        key: Redis key.

    Returns:
        str: Cached value or empty string.
    """
    try:
        return str(redis_client.get(key) or "").strip()
    except Exception:
        _LOGGER.exception("failed to read redis key for ingress image: key=%s", key)
        return ""


def resolve_attachment_image_url(
    attachment: dict[str, Any],
    payload_obj: dict[str, Any],
    *,
    redis_client: Any | None = None,
    redis_get: Optional[Callable[[str], str]] = None,
) -> str:
    """
    Resolve one attachment to a vision-compatible image URL.

    Supports ``url``, ``blob_ref`` (Redis lookup), ``base64`` data-URLs, and
    ``telegram_media_ref`` metadata fallback on the payload.

    Args:
        attachment: Image attachment dict.
        payload_obj: Full ingress payload for metadata fallbacks.
        redis_client: Optional Redis client for blob_ref resolution.
        redis_get: Optional callable override for Redis reads (tests).

    Returns:
        str: Resolved URL or empty string when not resolvable.
    """
    if not is_image_attachment(attachment):
        return ""

    source = attachment.get("source") or {}
    if not isinstance(source, dict):
        return ""

    source_kind = str(source.get("kind") or "").strip().lower()
    source_value = str(source.get("value") or "").strip()
    mime_type = str(attachment.get("mime_type") or "image/jpeg").strip()

    getter = redis_get
    if getter is None and redis_client is not None:
        getter = lambda key: _redis_get(redis_client, key)

    if source_kind == "url" and source_value.lower().startswith(("http://", "https://")):
        return source_value

    if source_kind == "base64" and source_value:
        try:
            base64.b64decode(source_value, validate=True)
        except (binascii.Error, ValueError):
            _LOGGER.warning("invalid base64 image attachment: attachment_id=%s", attachment.get("id"))
            return ""
        return f"data:{mime_type};base64,{source_value}"

    if source_kind == "blob_ref" and source_value and getter is not None:
        cached = getter(source_value)
        if cached.lower().startswith(("http://", "https://")):
            return cached

    metadata_obj = payload_obj.get("metadata") or {}
    extras_obj = metadata_obj.get("extras") if isinstance(metadata_obj, dict) else {}
    if isinstance(extras_obj, dict) and getter is not None:
        telegram_media_ref = str(extras_obj.get("telegram_media_ref") or "").strip()
        if telegram_media_ref:
            cached = getter(telegram_media_ref)
            if cached.lower().startswith(("http://", "https://")):
                return cached

    return ""


def resolve_image_urls_from_payload(
    payload_obj: dict[str, Any],
    *,
    redis_client: Any | None = None,
    redis_get: Optional[Callable[[str], str]] = None,
) -> list[ResolvedIngressImage]:
    """
    Resolve all image attachments in a payload to vision-compatible URLs.

    Args:
        payload_obj: Ingress payload object.
        redis_client: Optional Redis client for blob_ref resolution.
        redis_get: Optional callable override for Redis reads (tests).

    Returns:
        list[ResolvedIngressImage]: Resolved images in attachment order.
    """
    attachments = (payload_obj.get("content") or {}).get("attachments") or []
    if not isinstance(attachments, list):
        return []

    resolved: list[ResolvedIngressImage] = []
    image_index = 0
    for attachment in attachments:
        if not isinstance(attachment, dict) or not is_image_attachment(attachment):
            continue

        image_url = resolve_attachment_image_url(
            attachment,
            payload_obj,
            redis_client=redis_client,
            redis_get=redis_get,
        )
        if not image_url:
            continue

        attachment_id = str(attachment.get("id") or f"image-{image_index}").strip()
        resolved.append(
            ResolvedIngressImage(
                attachment_id=attachment_id,
                image_url=image_url,
                mime_type=str(attachment.get("mime_type") or ""),
                index=image_index,
            )
        )
        image_index += 1

    return resolved


def resolve_first_image_url_from_payload(
    payload_obj: dict[str, Any],
    *,
    redis_client: Any | None = None,
    redis_get: Optional[Callable[[str], str]] = None,
) -> str:
    """
    Resolve the first image URL from a payload (MedGemma enqueue compatibility).

    Args:
        payload_obj: Ingress payload object.
        redis_client: Optional Redis client.
        redis_get: Optional Redis read override.

    Returns:
        str: First resolvable image URL or empty string.
    """
    images = resolve_image_urls_from_payload(
        payload_obj,
        redis_client=redis_client,
        redis_get=redis_get,
    )
    if not images:
        return ""
    return images[0].image_url


def payload_has_image_attachment(payload: dict[str, Any]) -> bool:
    """
    Return True when the payload includes at least one image attachment source.

    Args:
        payload: Ingress payload dict.

    Returns:
        bool: True when an image attachment with a non-empty source value exists.
    """
    attachments = (payload.get("content") or {}).get("attachments") or []
    if not isinstance(attachments, list):
        return False
    for attachment in attachments:
        if not isinstance(attachment, dict) or not is_image_attachment(attachment):
            continue
        source = attachment.get("source") or {}
        if isinstance(source, dict) and str(source.get("value") or "").strip():
            return True
    return False

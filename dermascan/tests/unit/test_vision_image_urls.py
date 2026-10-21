"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for vision image URL refresh helpers."""


from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from dermascan.shared.vision_image_urls import (
    CHECKPOINT_IMAGE_BLOCK_TYPE,
    build_checkpoint_image_block,
    is_invalid_image_url_error,
    refresh_message_vision_urls,
    refresh_vision_image_url,
    resolve_client_media_blob_path,
)

_FIREBASE_URL = (
    "https://firebasestorage.googleapis.com/v0/b/dawagpn.appspot.com/o/"
    "client-media%2F5350968932%2Fabc.jpg?alt=media&token=expired"
)
_BLOB_PATH = "client-media/5350968932/abc.jpg"
_FRESH_URL = "https://firebasestorage.googleapis.com/v0/b/dawagpn.appspot.com/o/fresh"


@pytest.mark.unit
class TestResolveClientMediaBlobPath:
    """Blob path normalization for vision refresh."""

    def test_accepts_durable_path(self) -> None:
        assert resolve_client_media_blob_path(_BLOB_PATH) == _BLOB_PATH

    def test_extracts_path_from_firebase_url(self) -> None:
        assert resolve_client_media_blob_path(_FIREBASE_URL) == _BLOB_PATH

    def test_rejects_external_url(self) -> None:
        assert resolve_client_media_blob_path("https://example.com/a.jpg") == ""


@pytest.mark.unit
class TestRefreshVisionImageUrl:
    """Signed URL refresh behavior."""

    def test_passes_through_data_url(self) -> None:
        data_url = "data:image/jpeg;base64,abc"
        assert refresh_vision_image_url(data_url) == data_url

    def test_passes_through_external_url(self) -> None:
        external = "https://example.com/a.jpg"
        assert refresh_vision_image_url(external) == external

    @patch("dermascan.shared.vision_image_urls.sign_client_image_blob_path")
    def test_resigns_client_media_url(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        assert refresh_vision_image_url(_FIREBASE_URL) == _FRESH_URL
        sign_mock.assert_called_once_with(_BLOB_PATH)

    @patch("dermascan.shared.vision_image_urls.sign_client_image_blob_path")
    def test_returns_empty_when_resign_fails(self, sign_mock) -> None:
        sign_mock.side_effect = FileNotFoundError("missing")
        assert refresh_vision_image_url(_FIREBASE_URL) == ""

    @patch("dermascan.shared.vision_image_urls.sign_client_image_blob_path")
    def test_resigns_bare_blob_path(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        assert refresh_vision_image_url(_BLOB_PATH) == _FRESH_URL
        sign_mock.assert_called_once_with(_BLOB_PATH)


@pytest.mark.unit
class TestCheckpointImageBlocks:
    """Durable checkpoint image block helpers."""

    def test_build_checkpoint_image_block(self) -> None:
        block = build_checkpoint_image_block(_BLOB_PATH)
        assert block["type"] == CHECKPOINT_IMAGE_BLOCK_TYPE
        assert block["blob_path"] == _BLOB_PATH

    @patch("dermascan.shared.vision_image_urls.sign_client_image_blob_path")
    def test_refresh_converts_image_blob_block(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        message = HumanMessage(
            content=[
                {"type": "text", "text": "inspect this"},
                build_checkpoint_image_block(_BLOB_PATH),
            ]
        )
        updated = refresh_message_vision_urls(message)
        assert updated.content[1]["type"] == "image_url"
        assert updated.content[1]["image_url"]["url"] == _FRESH_URL


@pytest.mark.unit
class TestRefreshMessageVisionUrls:
    """Multimodal message refresh."""

    @patch("dermascan.shared.vision_image_urls.refresh_vision_image_url")
    def test_refreshes_image_block(self, refresh_mock) -> None:
        refresh_mock.return_value = _FRESH_URL
        message = HumanMessage(
            content=[
                {"type": "text", "text": "inspect this"},
                {"type": "image_url", "image_url": {"url": _FIREBASE_URL}},
            ]
        )
        updated = refresh_message_vision_urls(message)
        assert updated is not message
        assert updated.content[1]["image_url"]["url"] == _FRESH_URL

    @patch("dermascan.shared.vision_image_urls.refresh_vision_image_url")
    def test_replaces_unrecoverable_image_with_text(self, refresh_mock) -> None:
        refresh_mock.return_value = ""
        message = HumanMessage(
            content=[
                {"type": "image_url", "image_url": {"url": _FIREBASE_URL}},
            ]
        )
        updated = refresh_message_vision_urls(message)
        assert updated.content[0]["type"] == "text"
        assert "unavailable" in updated.content[0]["text"].lower()


@pytest.mark.unit
class TestInvalidImageUrlError:
    """Checkpoint poison detection for stale vision URLs."""

    def test_detects_openai_invalid_image_url(self) -> None:
        exc = Exception('Error code: 400 - {"error":{"code":"invalid_image_url"}}')
        assert is_invalid_image_url_error(exc) is True

    def test_ignores_unrelated_errors(self) -> None:
        assert is_invalid_image_url_error(Exception("recursion limit")) is False

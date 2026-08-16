"""Unit tests for ingress payload image resolution."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from dermascan.shared.ingress_payload import (
    payload_has_image_attachment,
    resolve_attachment_blob_path,
    resolve_first_image_blob_path_from_payload,
    resolve_first_image_url_from_payload,
    resolve_image_urls_from_payload,
)

_BLOB_PATH = "client-media/wa:123/abc.jpg"
_FIREBASE_URL = (
    "https://firebasestorage.googleapis.com/v0/b/dawagpn.appspot.com/o/"
    "client-media%2Fwa%3A123%2Fabc.jpg?alt=media&token=expired"
)
_FRESH_URL = "https://firebasestorage.googleapis.com/v0/b/dawagpn.appspot.com/o/fresh"


@pytest.mark.unit
class TestIngressPayloadImages:
    """Image URL resolution from ingress attachments."""

    def test_resolve_external_https_url(self) -> None:
        payload = {
            "content": {
                "attachments": [
                    {
                        "id": "img-1",
                        "type": "image",
                        "mime_type": "image/jpeg",
                        "source": {"kind": "url", "value": "https://example.com/a.jpg"},
                    }
                ]
            }
        }
        images = resolve_image_urls_from_payload(payload)
        assert len(images) == 1
        assert images[0].image_url == "https://example.com/a.jpg"
        assert images[0].blob_path == ""
        assert images[0].attachment_id == "img-1"

    @patch("dermascan.shared.ingress_payload.sign_client_image_blob_path")
    def test_resolve_blob_ref_via_redis_get(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        payload = {
            "content": {
                "attachments": [
                    {
                        "id": "img-2",
                        "type": "image",
                        "source": {"kind": "blob_ref", "value": "blob:ref-1"},
                    }
                ]
            }
        }
        redis_values = {"blob:ref-1": _BLOB_PATH}

        def getter(key: str) -> str:
            return redis_values.get(key, "")

        images = resolve_image_urls_from_payload(payload, redis_get=getter)
        assert len(images) == 1
        assert images[0].image_url == _FRESH_URL
        assert images[0].blob_path == _BLOB_PATH
        sign_mock.assert_called_once_with(_BLOB_PATH)

    @patch("dermascan.shared.ingress_payload.sign_client_image_blob_path")
    def test_resolve_legacy_signed_url_in_redis(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        payload = {
            "content": {
                "attachments": [
                    {
                        "id": "img-legacy",
                        "type": "image",
                        "source": {"kind": "blob_ref", "value": "media:telegram:1:abc"},
                    }
                ]
            }
        }
        redis_values = {"media:telegram:1:abc": _FIREBASE_URL}

        def getter(key: str) -> str:
            return redis_values.get(key, "")

        blob_path = resolve_attachment_blob_path(payload["content"]["attachments"][0], payload, redis_get=getter)
        assert blob_path == _BLOB_PATH

        images = resolve_image_urls_from_payload(payload, redis_get=getter)
        assert images[0].blob_path == _BLOB_PATH
        assert images[0].image_url == _FRESH_URL

    @patch("dermascan.shared.ingress_payload.sign_client_image_blob_path")
    def test_resigns_firebase_url_attachment(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        payload = {
            "content": {
                "attachments": [
                    {
                        "id": "img-firebase",
                        "type": "image",
                        "source": {"kind": "url", "value": _FIREBASE_URL},
                    }
                ]
            }
        }
        images = resolve_image_urls_from_payload(payload)
        assert images[0].blob_path == _BLOB_PATH
        assert images[0].image_url == _FRESH_URL

    def test_resolve_base64_data_url(self) -> None:
        raw = base64.b64encode(b"fake-image-bytes").decode("ascii")
        payload = {
            "content": {
                "attachments": [
                    {
                        "id": "img-3",
                        "type": "image",
                        "mime_type": "image/png",
                        "source": {"kind": "base64", "value": raw},
                    }
                ]
            }
        }
        images = resolve_image_urls_from_payload(payload)
        assert len(images) == 1
        assert images[0].image_url.startswith("data:image/png;base64,")

    def test_first_image_url_helper(self) -> None:
        payload = {
            "content": {
                "attachments": [
                    {
                        "type": "image",
                        "source": {"kind": "url", "value": "https://example.com/one.jpg"},
                    },
                    {
                        "type": "image",
                        "source": {"kind": "url", "value": "https://example.com/two.jpg"},
                    },
                ]
            }
        }
        assert resolve_first_image_url_from_payload(payload) == "https://example.com/one.jpg"

    @patch("dermascan.shared.ingress_payload.sign_client_image_blob_path")
    def test_first_image_blob_path_helper(self, sign_mock) -> None:
        sign_mock.return_value = _FRESH_URL
        payload = {
            "content": {
                "attachments": [
                    {
                        "type": "image",
                        "source": {"kind": "blob_ref", "value": "blob:ref-1"},
                    }
                ]
            }
        }
        redis_values = {"blob:ref-1": _BLOB_PATH}

        def getter(key: str) -> str:
            return redis_values.get(key, "")

        assert resolve_first_image_blob_path_from_payload(payload, redis_get=getter) == _BLOB_PATH

    def test_payload_has_image_attachment(self) -> None:
        assert payload_has_image_attachment(
            {"content": {"attachments": [{"type": "image", "source": {"kind": "url", "value": "x"}}]}}
        )
        assert not payload_has_image_attachment({"content": {"attachments": []}})

    def test_skips_non_image_attachments(self) -> None:
        payload = {
            "content": {
                "attachments": [
                    {
                        "type": "document",
                        "source": {"kind": "url", "value": "https://example.com/doc.pdf"},
                    },
                    {
                        "id": "skin",
                        "type": "image",
                        "source": {"kind": "url", "value": "https://example.com/skin.jpg"},
                    },
                ]
            }
        }
        images = resolve_image_urls_from_payload(payload)
        assert len(images) == 1
        assert images[0].attachment_id == "skin"

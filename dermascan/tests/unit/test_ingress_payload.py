"""Unit tests for ingress payload image resolution."""

from __future__ import annotations

import base64

import pytest

from dermascan.shared.ingress_payload import (
    payload_has_image_attachment,
    resolve_attachment_image_url,
    resolve_first_image_url_from_payload,
    resolve_image_urls_from_payload,
)


@pytest.mark.unit
class TestIngressPayloadImages:
    """Image URL resolution from ingress attachments."""

    def test_resolve_https_url(self) -> None:
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
        assert images[0].attachment_id == "img-1"

    def test_resolve_blob_ref_via_redis_get(self) -> None:
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
        redis_values = {"blob:ref-1": "https://cdn.example.com/signed.jpg"}

        def getter(key: str) -> str:
            return redis_values.get(key, "")

        images = resolve_image_urls_from_payload(payload, redis_get=getter)
        assert len(images) == 1
        assert images[0].image_url == "https://cdn.example.com/signed.jpg"

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

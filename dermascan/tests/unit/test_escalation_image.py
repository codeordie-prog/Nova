"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for escalation image blob path resolution."""


from __future__ import annotations

from dermascan.shared.escalation_image import (
    normalize_image_blob_path,
    resolve_escalation_image_blob_path,
)


class _FakeRedis:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def test_normalize_image_blob_path_accepts_client_media_prefix() -> None:
    path = "client-media/wa:123/abc.jpg"
    assert normalize_image_blob_path(path) == path


def test_normalize_image_blob_path_extracts_firebase_url() -> None:
    url = (
        "https://storage.googleapis.com/bucket/client-media/wa%3A123/abc.jpg"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
    )
    assert normalize_image_blob_path(url) == "client-media/wa:123/abc.jpg"


def test_resolve_escalation_image_blob_path_falls_back_to_ingress_cache() -> None:
    request_id = "req-image-1"
    envelope = (
        '{"request_id":"req-image-1","payload":"{\\"content\\":{\\"attachments\\":[{\\"type\\":\\"image\\",'
        '\\"mime_type\\":\\"image/jpeg\\",\\"source\\":{\\"kind\\":\\"url\\",\\"value\\":'
        '\\"https://storage.googleapis.com/bucket/client-media/wa%3A123/photo.jpg\\"}}]}}"}'
    )
    redis_client = _FakeRedis({f"orchestrator:ingress_payload:{request_id}": envelope})

    resolved = resolve_escalation_image_blob_path(
        redis_client,
        image_blob_path="",
        request_id=request_id,
        case_id="case-1",
    )

    assert resolved == "client-media/wa:123/photo.jpg"

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for skin audit Redis persistence helpers."""


from __future__ import annotations

import json

from typing import Any

import pytest

from dermascan.shared import skin_audit_store


@pytest.mark.unit
class TestSkinAuditPersistence:
    """Persist and load skin audit snapshots via Redis."""

    @staticmethod
    def _patch_redis(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
        monkeypatch.setattr(
            skin_audit_store,
            "get_redis_client",
            lambda *, decode_responses=True, refresh=False: fake_redis,
        )
        monkeypatch.setattr(
            skin_audit_store,
            "run_with_redis_retries",
            lambda operation, **_: operation(),
        )

    def test_persist_and_load_round_trip(self, fake_redis, monkeypatch) -> None:
        self._patch_redis(monkeypatch, fake_redis)

        audit = {
            "primary_concern": "acne-prone appearance",
            "current_condition": {"severity": "moderate", "active_inflammation": True},
            "needs": {"barrier_repair": True},
            "confidence": 0.0,
        }
        assert skin_audit_store.persist_skin_audit_snapshot(
            "client-1",
            skin_audit=audit,
            request_id="req-1",
            primary_observation="acne-prone appearance",
            risk_tier="watch",
            image_quality="good",
            signal_confidence="0.82",
            snapshot_at_ts=1_700_000_000,
        )

        loaded, updated_at_ts = skin_audit_store.load_latest_skin_audit("client-1")
        assert loaded["primary_concern"] == "acne-prone appearance"
        assert loaded["confidence"] == pytest.approx(0.82)
        assert loaded["risk_tier"] == "watch"
        assert loaded["image_quality"] == "good"
        assert loaded["primary_observation"] == "acne-prone appearance"
        assert loaded["snapshot_at_ts"] == 1_700_000_000
        assert updated_at_ts == 1_700_000_000

    def test_idempotent_on_same_request_id(self, fake_redis, monkeypatch) -> None:
        self._patch_redis(monkeypatch, fake_redis)

        audit = {"primary_concern": "dry appearance", "confidence": 0.0}
        skin_audit_store.persist_skin_audit_snapshot(
            "client-2",
            skin_audit=audit,
            request_id="req-dup",
            signal_confidence="0.55",
            snapshot_at_ts=100,
        )
        skin_audit_store.persist_skin_audit_snapshot(
            "client-2",
            skin_audit={"primary_concern": "changed", "confidence": 0.99},
            request_id="req-dup",
            signal_confidence="0.99",
            snapshot_at_ts=200,
        )

        loaded, _ = skin_audit_store.load_latest_skin_audit("client-2")
        assert loaded["primary_concern"] == "dry appearance"
        assert loaded["confidence"] == pytest.approx(0.55)

    def test_fail_open_on_redis_error(self, monkeypatch) -> None:
        def _boom(*_: object, **__: object) -> None:
            raise ConnectionError("redis down")

        monkeypatch.setattr(skin_audit_store, "get_redis_client", _boom)

        assert skin_audit_store.persist_skin_audit_snapshot(
            "client-3",
            skin_audit={"primary_concern": "redness"},
            request_id="req-3",
        ) is False

        loaded, updated_at_ts = skin_audit_store.load_latest_skin_audit("client-3")
        assert loaded == {}
        assert updated_at_ts is None

    def test_uses_dedicated_skin_audit_key(self, fake_redis, monkeypatch) -> None:
        self._patch_redis(monkeypatch, fake_redis)

        skin_audit_store.persist_skin_audit_snapshot(
            "client-4",
            skin_audit={"primary_concern": "visible redness"},
            request_id="req-4",
            snapshot_at_ts=42,
        )

        raw = fake_redis.get("skin_audit:client-4")
        assert raw is not None
        envelope = json.loads(str(raw))
        assert envelope["schema_version"] == "cosmetic_v1"
        assert envelope["request_id"] == "req-4"
        assert fake_redis.get("skin_profile:client-4") is None

    def test_parse_signal_confidence(self) -> None:
        assert skin_audit_store.parse_signal_confidence("0.75") == pytest.approx(0.75)
        assert skin_audit_store.parse_signal_confidence("") == 0.0
        assert skin_audit_store.parse_signal_confidence("bad") == 0.0
        assert skin_audit_store.parse_signal_confidence("1.5") == pytest.approx(1.0)

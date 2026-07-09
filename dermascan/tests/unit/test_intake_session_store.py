"""Unit tests for intake session Redis persistence."""

from __future__ import annotations

from typing import Any

import pytest

from dermascan.shared.intake_session import (
    IntakeImageCategory,
    IntakeImageRef,
    IntakePhase,
    IntakeSession,
    ServiceIntent,
    intake_session_archive_key,
    intake_session_key,
)
from dermascan.shared.intake_session_store import (
    IntakeSessionStoreError,
    archive_intake_session,
    create_intake_session,
    deserialize_intake_session,
    delete_intake_session,
    get_or_create_intake_session,
    load_archived_intake_session,
    load_intake_session,
    save_intake_session,
    save_intake_session_recomputed,
    serialize_intake_session,
)


@pytest.mark.unit
class TestSerialization:
    """JSON round-trip for intake sessions."""

    def test_serialize_deserialize_round_trip(self) -> None:
        session = IntakeSession(
            client_id="c1",
            case_id="case-1",
            phase=IntakePhase.CONSULTATION,
            service_intent=ServiceIntent.PRODUCT_REVIEW,
            images={
                IntakeImageCategory.PRODUCT.value: IntakeImageRef(
                    request_id="req-1",
                    image_url="https://example.com/p.jpg",
                ),
            },
        )
        raw = serialize_intake_session(session)
        parsed = deserialize_intake_session(raw)
        assert parsed is not None
        assert parsed.client_id == "c1"
        assert parsed.service_intent == ServiceIntent.PRODUCT_REVIEW
        assert parsed.images[IntakeImageCategory.PRODUCT.value].request_id == "req-1"


@pytest.mark.unit
class TestIntakeSessionStore:
    """Redis load/save helpers using fakeredis."""

    def test_create_and_load(self, fake_redis: Any) -> None:
        session = create_intake_session("client-a", "case-1", redis_client=fake_redis)
        assert session.client_id == "client-a"
        assert session.case_id == "case-1"

        loaded = load_intake_session("client-a", redis_client=fake_redis)
        assert loaded is not None
        assert loaded.case_id == "case-1"
        assert loaded.phase == IntakePhase.GREETING

    def test_get_or_create_returns_existing(self, fake_redis: Any) -> None:
        first = get_or_create_intake_session("client-b", "case-1", redis_client=fake_redis)
        first = first.model_copy(
            update={
                "service_intent": ServiceIntent.SKIN_REVIEW,
                "primary_concern": "rash",
            }
        )
        save_intake_session(first, redis_client=fake_redis)

        second = get_or_create_intake_session("client-b", "case-1", redis_client=fake_redis)
        assert second.service_intent == ServiceIntent.SKIN_REVIEW
        assert second.primary_concern == "rash"

    def test_get_or_create_updates_case_id(self, fake_redis: Any) -> None:
        get_or_create_intake_session("client-c", "case-old", redis_client=fake_redis)
        updated = get_or_create_intake_session("client-c", "case-new", redis_client=fake_redis)
        assert updated.case_id == "case-new"

        raw = fake_redis.get(intake_session_key("client-c"))
        parsed = deserialize_intake_session(raw)
        assert parsed is not None
        assert parsed.case_id == "case-new"

    def test_delete_intake_session(self, fake_redis: Any) -> None:
        create_intake_session("client-d", "case-1", redis_client=fake_redis)
        assert delete_intake_session("client-d", redis_client=fake_redis)
        assert load_intake_session("client-d", redis_client=fake_redis) is None

    def test_save_recomputed_persists_summary(self, fake_redis: Any) -> None:
        session = IntakeSession(
            client_id="client-e",
            case_id="case-1",
            service_intent=ServiceIntent.SKIN_REVIEW,
            primary_concern="Itchy rash",
            reproductive_status_confirmed=True,
            images={
                IntakeImageCategory.SKIN.value: IntakeImageRef(
                    request_id="req-s",
                    image_url="https://example.com/s.jpg",
                ),
            },
            created_at_ms=1000,
            updated_at_ms=1000,
        )
        profile = {
            "age": 25,
            "gender": "female",
            "location": "Nairobi",
            "current_products": ["cleanser"],
            "current_goals": ["relief"],
            "allergies": ["nuts"],
        }
        final = save_intake_session_recomputed(
            session,
            profile,
            redis_client=fake_redis,
            now_ms=2000,
        )
        assert final.phase == IntakePhase.READY_TO_ROUTE
        assert "Itchy rash" in final.collected_context_summary

        loaded = load_intake_session("client-e", redis_client=fake_redis)
        assert loaded is not None
        assert loaded.phase == IntakePhase.READY_TO_ROUTE
        assert loaded.collected_context_summary == final.collected_context_summary

    def test_archive_and_load_archived(self, fake_redis: Any) -> None:
        session = create_intake_session("client-f", "case-archive", redis_client=fake_redis)
        archive_intake_session(session, redis_client=fake_redis, delete_active=True)

        assert load_intake_session("client-f", redis_client=fake_redis) is None

        archived = load_archived_intake_session("case-archive", redis_client=fake_redis)
        assert archived is not None
        assert archived.phase == IntakePhase.DELIVERED
        assert fake_redis.get(intake_session_archive_key("case-archive")) is not None

    def test_save_raises_when_redis_fails(self, fake_redis: Any) -> None:
        session = IntakeSession(client_id="client-g", case_id="case-1")

        class BrokenRedis:
            def setex(self, *_args: Any, **_kwargs: Any) -> None:
                raise ConnectionError("broken")

        with pytest.raises(IntakeSessionStoreError):
            save_intake_session(session, redis_client=BrokenRedis())

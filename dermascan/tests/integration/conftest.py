"""Integration-test fixtures (real Redis via testcontainers)."""

from __future__ import annotations

import pytest

from dermascan.tests.fixtures.stream_helpers import unique_stream_bundle


@pytest.fixture
def stream_bundle() -> dict[str, str]:
    """Unique stream + consumer group + DLQ names per test."""
    return unique_stream_bundle()


@pytest.fixture
def billing_db(monkeypatch: pytest.MonkeyPatch):
    """
    A throwaway Postgres database with the billing schema migrated in.

    Uses the local Postgres server rather than a container so the suite stays
    runnable without Docker. Skips when no server is reachable.
    """
    import os
    import uuid

    psycopg = pytest.importorskip("psycopg")

    host = os.getenv("BILLING_TEST_PG_HOST", "localhost")
    port = os.getenv("BILLING_TEST_PG_PORT", "5432")
    user = os.getenv("BILLING_TEST_PG_USER", os.getenv("USER", "postgres"))
    password = os.getenv("BILLING_TEST_PG_PASSWORD", "ignored")
    admin_db = os.getenv("BILLING_TEST_PG_ADMIN_DB", "postgres")
    name = f"billing_test_{uuid.uuid4().hex[:12]}"

    admin_conninfo = (
        f"host={host} port={port} user={user} dbname={admin_db} "
        f"password={password} connect_timeout=5"
    )
    try:
        admin = psycopg.connect(admin_conninfo, autocommit=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no local Postgres for billing tests: {exc}")

    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')

    monkeypatch.setenv("POSTGRES_HOST", host)
    monkeypatch.setenv("POSTGRES_PORT", port)
    monkeypatch.setenv("POSTGRES_DATABASE", name)
    monkeypatch.setenv("POSTGRES_USER", user)
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    monkeypatch.setenv("POSTGRES_SSLMODE", "")

    from dermascan.billing import db as billing_db_module
    from dermascan.billing import pricing
    from dermascan.scripts.migrate import run_migrations

    billing_db_module.reset_pool()
    pricing.clear_cache()
    run_migrations()

    try:
        yield name
    finally:
        billing_db_module.reset_pool()
        pricing.clear_cache()
        with psycopg.connect(admin_conninfo, autocommit=True) as cleanup:
            cleanup.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            cleanup.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture
def billing_redis(monkeypatch: pytest.MonkeyPatch):
    """In-memory Redis wired into every module that reaches for the shared client."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)

    for module in (
        "dermascan.billing.redis_wallet",
        "dermascan.billing.notify",
        "dermascan.billing.accounts",
    ):
        monkeypatch.setattr(f"{module}.get_redis_client", lambda **_: client, raising=True)
    return client

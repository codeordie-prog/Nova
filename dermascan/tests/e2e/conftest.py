"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Fixtures for live-stack end-to-end pipeline tests."""


from __future__ import annotations

from typing import Any

import pytest
from dotenv import load_dotenv

from dermascan.shared.env_paths import find_repo_root
from dermascan.tests.e2e.stack_helpers import (
    E2EConfig,
    build_redis_client,
    ensure_e2e_stack_ready,
    load_e2e_config,
    load_sample_image_bytes,
)


def pytest_configure(config: pytest.Config) -> None:
    """Load repo-root ``.env`` so E2E tests see the same vars as the live stack."""
    _ = config
    env_file = find_repo_root() / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Session-scoped E2E configuration from environment."""
    return load_e2e_config()


@pytest.fixture(scope="session")
def e2e_redis(e2e_config: E2EConfig) -> Any:
    """Redis client connected to the same REDIS_URL as the live stack."""
    ensure_e2e_stack_ready(e2e_config)
    return build_redis_client(e2e_config)


@pytest.fixture(scope="session")
def e2e_stack(e2e_config: E2EConfig, e2e_redis: Any) -> E2EConfig:
    """Ensure backend + Redis are up; return config for tests."""
    _ = e2e_redis
    return e2e_config


@pytest.fixture(scope="session")
def sample_image_bytes() -> list[bytes]:
    """Real JPEG bytes from ``dermascan/tests/images/``."""
    return load_sample_image_bytes()

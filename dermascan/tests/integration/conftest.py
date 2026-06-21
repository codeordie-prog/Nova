"""Integration-test fixtures (real Redis via testcontainers)."""

from __future__ import annotations

import pytest

from dermascan.tests.fixtures.stream_helpers import unique_stream_bundle


@pytest.fixture
def stream_bundle() -> dict[str, str]:
    """Unique stream + consumer group + DLQ names per test."""
    return unique_stream_bundle()

"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Load repo ``.env`` for live API/load probes (ingress secret, REDIS_URL, etc.)."""


from __future__ import annotations

import pytest
from dotenv import load_dotenv

from dermascan.shared.env_paths import find_repo_root


def pytest_configure(config: pytest.Config) -> None:
    """Load repo-root ``.env`` before API probe tests read os.environ."""
    _ = config
    env_file = find_repo_root() / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

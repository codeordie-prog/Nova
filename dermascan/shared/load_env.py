"""
Bootstrap environment variables for local and Cloud Run runtimes.

Local dev loads ``.env`` from the repo root. Cloud Run mounts a synced copy at
``/secrets/env/.env`` (Secret Manager) and sets ``DERMASCAN_ENV_FILE``; deploy-time
overrides (API URL, MedGemma gateway URL) are injected by Cloud Run first and
are not overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    """
    Resolve repository root (directory containing ``pyproject.toml``).

    Returns:
        Path: Repo root path.
    """
    explicit = os.getenv("DERMASCAN_ROOT", "").strip()
    if explicit:
        return Path(explicit).resolve()
    cursor = Path(__file__).resolve().parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


def bootstrap_env() -> None:
    """
    Load environment from Cloud Run secret file and/or local ``.env``.

    Cloud Run injects deploy overrides (URLs, ``DERMASCAN_ENV_FILE``) before
    Python starts; ``override=False`` preserves those values.
    """
    cloud_env_file = os.getenv("DERMASCAN_ENV_FILE", "/secrets/env/.env").strip()
    if cloud_env_file and os.path.isfile(cloud_env_file):
        load_dotenv(cloud_env_file, override=False)

    local_env = _repo_root() / ".env"
    if local_env.is_file():
        load_dotenv(local_env, override=False)

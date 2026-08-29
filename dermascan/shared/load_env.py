"""
Bootstrap environment variables for local and Cloud Run runtimes.

Local dev loads ``.env`` from the repo root, and ``.env.local`` on top of it
when present -- the overlay that points Redis and Postgres at the local stack
(see ``dermascan/scripts/local_stack.sh``). Cloud Run mounts a synced copy at
``/secrets/env/.env`` (Secret Manager) and sets ``DERMASCAN_ENV_FILE``; deploy-time
overrides (API URL, MedGemma gateway URL) are injected by Cloud Run first and
are not overwritten.

When ``TEST_MODE=true``, ``REDIS_URL`` is redirected to ``REDIS_TEST_URL`` (or
built from ``REDIS_TEST_HOST`` / ``REDIS_TEST_PORT`` / credentials) so local test
runs do not share queues with the live Redis instance.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_ENV_LOADED: bool = False


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


def is_truthy_env(value: str) -> bool:
    """
    Return True for common affirmative environment string values.

    Args:
        value: Raw environment variable value.

    Returns:
        bool: True when the value means "enabled".
    """
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_test_mode() -> bool:
    """
    Return True when ``TEST_MODE`` is enabled in the environment.

    Returns:
        bool: True when local test isolation is active.
    """
    return is_truthy_env(os.getenv("TEST_MODE", ""))


def build_redis_test_url() -> str:
    """
    Resolve the test Redis URL from ``REDIS_TEST_URL`` or component variables.

    Returns:
        str: Test Redis URL, or empty string when not configured.
    """
    explicit = os.getenv("REDIS_TEST_URL", "").strip()
    if explicit:
        return explicit

    host = os.getenv("REDIS_TEST_HOST", "").strip()
    port = os.getenv("REDIS_TEST_PORT", "").strip()
    password = os.getenv("REDIS_TEST_PASSWORD", "").strip()
    username = os.getenv("REDIS_TEST_USERNAME", "default").strip() or "default"
    if host and port and password:
        return f"redis://{username}:{password}@{host}:{port}"
    return ""


def resolve_redis_url() -> str:
    """
    Return the effective Redis URL, honoring ``TEST_MODE`` when enabled.

    Returns:
        str: Redis connection URL for the active mode.
    """
    if is_test_mode():
        test_url = build_redis_test_url()
        if test_url:
            return test_url
    return os.getenv("REDIS_URL", "").strip()


def apply_test_mode_overrides() -> None:
    """
    Apply environment overrides for local test mode.

    When ``TEST_MODE`` is enabled, sets ``REDIS_URL`` to the test instance URL
    without modifying live Redis settings stored in ``.env``.
    """
    if not is_test_mode():
        return

    test_url = build_redis_test_url()
    if not test_url:
        logger.warning(
            "TEST_MODE is enabled but REDIS_TEST_URL (or REDIS_TEST_HOST/PORT/PASSWORD) "
            "is not configured — REDIS_URL unchanged"
        )
    else:
        parsed = urlparse(test_url)
        host = parsed.hostname or "(unknown)"
        logger.info("TEST_MODE enabled: REDIS_URL -> test instance host=%s", host)
        os.environ["REDIS_URL"] = test_url

    test_bot_token = os.getenv("TELEGRAM_TEST_BOT_TOKEN", "").strip()
    if test_bot_token:
        live_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if live_bot_token != test_bot_token:
            logger.info("TEST_MODE enabled: TELEGRAM_BOT_TOKEN -> test bot token")
            os.environ["TELEGRAM_BOT_TOKEN"] = test_bot_token


def _load_env_files() -> None:
    """Load Cloud Run secret file and/or local ``.env`` without overwriting injected vars."""
    cloud_env_file = os.getenv("DERMASCAN_ENV_FILE", "/secrets/env/.env").strip()
    if cloud_env_file and os.path.isfile(cloud_env_file):
        load_dotenv(cloud_env_file, override=False)

    root = _repo_root()

    # `.env.local` is loaded first and, because every load is override=False,
    # the first value seen wins. It therefore overlays the local stack's Redis
    # and Postgres pointers on top of `.env` while every key it does not
    # mention -- API keys, model chains -- still comes from `.env`.
    overlay = root / ".env.local"
    if overlay.is_file():
        logger.info("local overlay active: .env.local")
        load_dotenv(overlay, override=False)

    local_env = root / ".env"
    if local_env.is_file():
        load_dotenv(local_env, override=False)


def ensure_env_loaded() -> None:
    """
    Load project environment files once and apply ``TEST_MODE`` overrides.

    Idempotent — safe to call from any entrypoint before reading ``REDIS_URL``.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        apply_test_mode_overrides()
        return

    _load_env_files()
    apply_test_mode_overrides()
    _ENV_LOADED = True


def bootstrap_env() -> None:
    """
    Load environment from Cloud Run secret file and/or local ``.env``.

    Cloud Run injects deploy overrides (URLs, ``DERMASCAN_ENV_FILE``) before
    Python starts; ``override=False`` preserves those values.
    """
    ensure_env_loaded()

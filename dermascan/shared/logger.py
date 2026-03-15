"""
Author: Dermascan Team
Date: 2026-03-12
License: MIT

Single application logger for tracing across backend, ai-service, and scripts.

Import this module once per process (or use :func:`get_logger`) to get a
consistent, configured logger. Configure log level via the ``LOG_LEVEL``
environment variable (e.g. ``DEBUG``, ``INFO``, ``WARNING``); default is ``INFO``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

# Defaults

DEFAULT_LEVEL: Final[str] = "INFO"
ENV_LOG_LEVEL: Final[str] = "LOG_LEVEL"


def _parse_level(level_name: str) -> int:
    """Map string level name to logging constant."""
    u: str = level_name.upper().strip()
    return getattr(logging, u, logging.INFO)


def get_logger(name: str = "dermascan") -> logging.Logger:
    """
    Return a configured logger for the given name (e.g. module ``__name__``).

    Uses a single shared handler and formatter so that all loggers created
    through this function have consistent format and destination (stderr).
    Level is read from the ``LOG_LEVEL`` environment variable; default is ``INFO``.

    Args:
        name: Logger name. Use ``__name__`` for per-module loggers, or a
            fixed name (e.g. ``"dermascan"``) for a single app-wide logger.

    Returns:
        A :class:`logging.Logger` instance with level and handler set.

    Example:
        >>> from dermascan.shared.logger import get_logger
        >>> log = get_logger(__name__)
        >>> log.info("Inference started")
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name: str = os.environ.get(ENV_LOG_LEVEL, DEFAULT_LEVEL)
    level: int = _parse_level(level_name)
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger

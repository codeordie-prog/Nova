"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-03-12
Licence: PolyForm Noncommercial License 1.0.0

Single application logger for tracing across backend, ai-service, and scripts.

Import this module once per process (or use :func:`get_logger`) to get a
consistent, configured logger. Configure log level via the ``LOG_LEVEL``
environment variable (e.g. ``DEBUG``, ``INFO``, ``WARNING``); default is ``INFO``.

Set ``LOG_FILE`` to also append every record to a file. The API, the workers and
the Telegram bridge run as three processes; pointing all three at one path puts
a whole end-to-end run in a single chronological trace, which is why the format
carries the process name and pid.
"""


from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Final

# Defaults

DEFAULT_LEVEL: Final[str] = "INFO"
ENV_LOG_LEVEL: Final[str] = "LOG_LEVEL"
ENV_LOG_FILE: Final[str] = "LOG_FILE"

_CONSOLE_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_FILE_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)-8s | %(processName)s:%(process)d | "
    "%(threadName)s | %(name)s | %(message)s"
)
_DATEFMT: Final[str] = "%Y-%m-%d %H:%M:%S"

# One handler instance shared by every logger in the process. A handler per
# logger would mean a file descriptor per logger, and their writes would
# interleave mid-line.
_file_handler: logging.Handler | None = None
_file_handler_resolved: bool = False


def _parse_level(level_name: str) -> int:
    """Map string level name to logging constant."""
    u: str = level_name.upper().strip()
    return getattr(logging, u, logging.INFO)


def _repo_root() -> pathlib.Path:
    """Return the directory containing ``pyproject.toml``, else the cwd."""
    explicit = os.environ.get("DERMASCAN_ROOT", "").strip()
    if explicit:
        return pathlib.Path(explicit).resolve()
    cursor = pathlib.Path(__file__).resolve().parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return pathlib.Path.cwd().resolve()


def _resolve_file_handler() -> logging.Handler | None:
    """
    Build the shared ``LOG_FILE`` handler, or None when the variable is unset.

    Resolved once per process. A path that cannot be opened is reported on
    stderr and downgraded to no file logging: losing the trace is better than
    refusing to start the worker.

    Returns:
        The shared file handler, or None when file logging is off.
    """
    global _file_handler, _file_handler_resolved
    if _file_handler_resolved:
        return _file_handler
    _file_handler_resolved = True

    target = os.environ.get(ENV_LOG_FILE, "").strip()
    if not target:
        return None

    path = pathlib.Path(target).expanduser()
    if not path.is_absolute():
        # The API, the workers and the bridge are started from different
        # shells; a relative path must mean the same file for all of them.
        path = _repo_root() / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        print(f"logger: cannot write LOG_FILE={target}: {exc}", file=sys.stderr)
        return None

    handler.setLevel(_parse_level(os.environ.get(ENV_LOG_LEVEL, DEFAULT_LEVEL)))
    handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATEFMT))
    _file_handler = handler
    return handler


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
    handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATEFMT))
    logger.addHandler(handler)

    file_handler = _resolve_file_handler()
    if file_handler is not None:
        logger.addHandler(file_handler)

    return logger

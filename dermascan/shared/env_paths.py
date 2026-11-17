"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Author: Dermascan Team
Date: 2026-06-17
Licence: PolyForm Noncommercial License 1.0.0

Description: Resolve filesystem paths from environment variables for local and deployed runs.

Paths in ``.env`` may be absolute, relative to the repository root, or relative
to ``DERMASCAN_ROOT`` when that variable is set explicitly on the host.

"""


from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


_REPO_MARKER = "pyproject.toml"


@lru_cache(maxsize=1)
def find_repo_root() -> Path:
    """
    Locate the repository root by searching for ``pyproject.toml``.

    Search order:
        1. ``DERMASCAN_ROOT`` when set
        2. Parents of this module's path
        3. Parents of the current working directory

    Returns:
        Absolute path to the repository root.

    Raises:
        RuntimeError: If the repository root cannot be determined.
    """
    explicit_root = os.environ.get("DERMASCAN_ROOT", "").strip()
    if explicit_root:
        root = Path(explicit_root).expanduser().resolve()
        if (root / _REPO_MARKER).is_file():
            return root
        raise RuntimeError(
            f"DERMASCAN_ROOT={explicit_root} does not contain {_REPO_MARKER}"
        )

    candidates: list[Path] = [Path(__file__).resolve()]
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    seen: set[Path] = set()

    for start in candidates:
        for parent in (start, *start.parents):
            if parent in seen:
                continue
            seen.add(parent)
            if (parent / _REPO_MARKER).is_file():
                return parent

    raise RuntimeError(
        f"Could not find repository root (missing {_REPO_MARKER}). "
        "Set DERMASCAN_ROOT to the directory that contains pyproject.toml."
    )


def resolve_env_path(raw_path: str | None, *, required: bool = False) -> str:
    """
    Resolve a config path from ``.env`` for the current environment.

    Supports:
        - Absolute paths (Unix or Windows)
        - Paths relative to ``DERMASCAN_ROOT`` / auto-detected repo root
        - ``~`` expansion

    Args:
        raw_path: Value from an environment variable (may be empty).
        required: When True, raise if the path is missing or the file does not exist.

    Returns:
        Resolved absolute path, or empty string when ``raw_path`` is empty and
        ``required`` is False.

    Raises:
        FileNotFoundError: When ``required`` is True and the file is missing.
        RuntimeError: When ``required`` is True and ``raw_path`` is empty.
    """
    cleaned = (raw_path or "").strip().strip('"').strip("'")
    if not cleaned:
        if required:
            raise RuntimeError("A required filesystem path environment variable is empty")
        return ""

    expanded = Path(cleaned).expanduser()
    if expanded.is_absolute():
        resolved = expanded.resolve()
    else:
        resolved = (find_repo_root() / expanded).resolve()

    if required and not resolved.is_file():
        raise FileNotFoundError(
            f"Required path not found: {resolved} (from env value {cleaned!r})"
        )

    return str(resolved)

"""
Author: DermaScan Team
Date: 2026-06-05
Description: Deploy the Qwen Modal app from a local script.

This script provides a stable, typed wrapper around:
    modal deploy dermascan/ai_service/modal/qwen_service/app.py

It validates repository structure, checks Modal CLI availability, and surfaces
deployment failures with actionable error messages.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path


logger: logging.Logger = logging.getLogger("deploy_qwen_app")


def _project_root() -> Path:
    """
    Resolve the repository root path.

    Returns:
        Path: Absolute path to repository root.
    """
    return Path(__file__).resolve().parents[2]


def _qwen_app_file(root: Path) -> Path:
    """
    Build the absolute path to the Qwen Modal app file.

    Args:
        root: Repository root directory.

    Returns:
        Path: Absolute file path to the Qwen Modal app module.
    """
    return root / "dermascan" / "ai_service" / "modal" / "qwen_service" / "app.py"


def _validate_inputs(root: Path, app_file: Path) -> None:
    """
    Validate deployment preconditions.

    Args:
        root: Repository root directory.
        app_file: Qwen Modal app file path.

    Raises:
        FileNotFoundError: If the expected app file does not exist.
        RuntimeError: If the Modal CLI is not available in PATH.
    """
    if not app_file.exists():
        raise FileNotFoundError(f"Qwen app file not found: {app_file}")

    if shutil.which("modal") is None:
        raise RuntimeError(
            "Modal CLI not found in PATH. Install/login first, then retry: "
            "`pip install modal` and `modal token new`."
        )

    pyproject_file: Path = root / "pyproject.toml"
    if not pyproject_file.exists():
        logger.warning("pyproject.toml not found at %s; continuing anyway", pyproject_file)


def deploy_qwen(*, dry_run: bool = False) -> int:
    """
    Deploy the Qwen Modal app.

    Args:
        dry_run: If True, print the command without executing it.

    Returns:
        int: Process exit code (0 on success, non-zero on failure).
    """
    root: Path = _project_root()
    app_file: Path = _qwen_app_file(root)

    try:
        _validate_inputs(root, app_file)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error(str(exc))
        return 2

    command: list[str] = ["modal", "deploy", str(app_file)]
    logger.info("Deploy command: %s", " ".join(command))

    if dry_run:
        logger.info("Dry run enabled: no deployment executed")
        return 0

    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            command,
            cwd=str(root),
            text=True,
            check=False,
        )
    except Exception:
        logger.exception("Failed to execute Modal deploy command")
        return 3

    if result.returncode != 0:
        logger.error("Qwen deployment failed with exit code %s", result.returncode)
        return result.returncode

    logger.info("Qwen deployment completed successfully")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    Args:
        argv: Optional explicit CLI arguments list.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Deploy the Qwen Modal app.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print deployment command without executing it.",
    )
    return parser.parse_args(argv)


def main() -> int:
    """
    Script entrypoint.

    Returns:
        int: Exit status code.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = _parse_args()
    return deploy_qwen(dry_run=bool(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())

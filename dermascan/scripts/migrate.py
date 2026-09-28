"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Forward-only SQL migration runner.

The repo had no migration mechanism before billing; ``cir_pdfs`` was created by
a hand-run script. This applies ``dermascan/migrations/*.sql`` in filename
order inside a transaction each, recording every applied file with a checksum
so an edited migration is caught rather than silently skipped.

Usage:
    poetry run python -m dermascan.scripts.migrate            # apply pending
    poetry run python -m dermascan.scripts.migrate --dry-run  # list pending
    poetry run python -m dermascan.scripts.migrate --stamp 001_billing
        # record a migration as applied without running SQL (hand-run Cloud SQL)
"""

from __future__ import annotations

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Final

from dermascan.billing.db import get_pool, reset_pool
from dermascan.shared.logger import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "migrations"

_BOOTSTRAP_SQL: Final[str] = """
CREATE SCHEMA IF NOT EXISTS billing;
CREATE TABLE IF NOT EXISTS billing.schema_migrations (
    version    text PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def _checksum(sql: str) -> str:
    """
    Hash a migration body.

    Args:
        sql: Full text of the migration file.

    Returns:
        str: Hex sha256 digest.
    """
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def discover_migrations() -> list[Path]:
    """
    List migration files in application order.

    Returns:
        list[Path]: ``.sql`` files sorted by filename.
    """
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _applied_checksums(conn) -> dict[str, str]:
    """
    Read the versions already applied.

    Args:
        conn: Open psycopg connection.

    Returns:
        dict[str, str]: version -> checksum.
    """
    rows = conn.execute(
        "SELECT version, checksum FROM billing.schema_migrations"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def stamp_migration(version: str) -> None:
    """
    Record a migration version as applied without executing its SQL.

    Use when the database was provisioned by hand and already matches that
    migration (for example ``001_billing`` on legacy ``qutis_db``).

    Args:
        version: Migration stem, e.g. ``001_billing``.

    Raises:
        FileNotFoundError: When no ``{version}.sql`` exists.
        RuntimeError: When the version is already applied with a different checksum.
    """
    path = MIGRATIONS_DIR / f"{version}.sql"
    if not path.is_file():
        raise FileNotFoundError(f"no migration file: {path}")

    sql = path.read_text(encoding="utf-8")
    digest = _checksum(sql)

    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(_BOOTSTRAP_SQL)
        conn.commit()
        applied = _applied_checksums(conn)
        previous = applied.get(version)
        if previous is not None:
            if previous != digest:
                raise RuntimeError(
                    f"migration {version} already applied with a different checksum"
                )
            logger.info("already stamped: %s", version)
            return
        conn.execute(
            "INSERT INTO billing.schema_migrations (version, checksum) VALUES (%s, %s)",
            (version, digest),
        )
        conn.commit()
    logger.info("stamped: %s", version)


def run_migrations(*, dry_run: bool = False) -> int:
    """
    Apply every pending migration.

    Args:
        dry_run: When True, report pending migrations without applying them.

    Returns:
        int: Number of migrations applied (or that would be applied).

    Raises:
        RuntimeError: When an already-applied migration file has changed.
    """
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(_BOOTSTRAP_SQL)
        conn.commit()
        applied = _applied_checksums(conn)

    pending: list[tuple[Path, str, str]] = []
    for path in discover_migrations():
        sql = path.read_text(encoding="utf-8")
        digest = _checksum(sql)
        version = path.stem
        previous = applied.get(version)
        if previous is None:
            pending.append((path, version, digest))
        elif previous != digest:
            raise RuntimeError(
                f"migration {version} was already applied but its contents changed; "
                "migrations are forward-only, add a new file instead"
            )

    if not pending:
        logger.info("no pending migrations")
        return 0

    if dry_run:
        for _, version, _ in pending:
            logger.info("pending: %s", version)
        return len(pending)

    for path, version, digest in pending:
        sql = path.read_text(encoding="utf-8")
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO billing.schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, digest),
                )
        logger.info("applied: %s", version)

    return len(pending)


def main() -> int:
    """
    CLI entry point.

    Returns:
        int: Process exit code.
    """
    parser = argparse.ArgumentParser(description="Apply pending billing migrations.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending migrations without applying them.",
    )
    parser.add_argument(
        "--stamp",
        metavar="VERSION",
        help="Record VERSION as applied without running SQL (hand-run databases).",
    )
    args = parser.parse_args()

    try:
        if args.stamp:
            stamp_migration(args.stamp)
            count = 0
        else:
            count = run_migrations(dry_run=args.dry_run)
    except Exception:
        logger.exception("migration run failed")
        return 1
    finally:
        # The pool spawns worker threads; leaving it open raises
        # PythonFinalizationError at interpreter shutdown.
        reset_pool()

    verb = "pending" if args.dry_run else "applied"
    logger.info("%s migration(s) %s", count, verb)
    return 0


if __name__ == "__main__":
    sys.exit(main())

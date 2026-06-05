"""
Author: Dermascan Team
Date: 2026-05-21
License: MIT

Description: Smoke test utilities for vector store integration tests.

"""

from __future__ import annotations

import os
import re
from logging import Logger
from typing import Any

import psycopg2

from dermascan.rules_engine.vector_store.build_vectorstore import VectorStore
from dermascan.shared.logger import get_logger

# Load environment variables
from dotenv import load_dotenv

load_dotenv()

# Initialize logger
logger: Logger = get_logger(__name__)

# For smoke test
POSTGRES_HOST = os.environ.get("postgres_host", "localhost")
POSTGRES_PORT = os.environ.get("postgres_port", "5432")
POSTGRES_DATABASE = os.environ.get("postgres_database", "postgres")
POSTGRES_USER = os.environ.get("postgres_user", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRESS_PASSWORD", "")


def _establish_postgres_conn():
    """
    Establish a connection to postgress.
    Used for smoke test.

    Returns:
        psycopg2 connection object

    Raises:
        ValueError: If postgres_password environment variable is not set
    """
    if not POSTGRES_PASSWORD:
        raise ValueError("postgres_password environment variable is required")

    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        database=POSTGRES_DATABASE,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def _normalize_lookup_name(raw_name: str) -> str:
    """
    Normalize ingredient name for robust DB lookup.
    """
    return "".join(ch.lower() for ch in raw_name if ch.isalnum())


def _fetch_smoke_test_records(
    *,
    limit: int = 3,
    table_name: str = "cir_pdfs",
    ingredient_names: list[str] | None = None,
) -> list[dict[str, str]]:
    """
    Fetch smoke-test ingredient records from Postgres.

    Requires rows that have a non-empty Firebase download_url.

    Args:
        limit: Maximum number of records to fetch
        table_name: Name of the table to query (default: "cir_pdfs")
        ingredient_names: Optional ingredient names to target for smoke test

    Returns:
        List of dictionaries containing record data

    Raises:
        ValueError: If table_name contains invalid characters or postgres_password is not set
    """
    # Check table name
    if not table_name.replace("_", "").isalnum():
        raise ValueError(f"Invalid table_name: {table_name}")

    requested_names = [
        _normalize_lookup_name(str(name).strip())
        for name in (ingredient_names or [])
        if str(name).strip()
    ]
    if requested_names:
        limit = max(limit, len(requested_names))

    # Establish postgres connection
    conn = _establish_postgres_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )

        # Get existing columns
        existing_columns = {row[0] for row in cur.fetchall()}

        def pick_col(*candidates: str) -> str | None:
            """
            Pick the first existing column from candidates.

            Args:
                *candidates: Column names to check

            Returns:
                First existing column name, or None if none exist
            """
            for cand in candidates:
                if cand in existing_columns:
                    return cand
            return None

        def col_expr(*candidates: str, fallback: str = "''") -> str:
            """
            Get the SQL expression for a column, using the first existing one from candidates.

            Args:
                *candidates: Column names to check
                fallback: Fallback value if no column exists

            Returns:
                SQL expression for the column
            """
            selected = pick_col(*candidates)
            return f'"{selected}"' if selected else fallback

        ingredient_id_expr = col_expr("ingredient_id", "Ingredient_ID", fallback="''")
        normalized_names_expr = col_expr("normalized_names", "Normalized_Names", fallback="''")
        pdf_name_expr = col_expr("pdf_name", "PDF_Name", fallback="''")
        ingredient_name_expr = col_expr("cir_ingredient_name", "CIR_Ingredient_Name", fallback="''")
        function_expr = col_expr("function", "Function", fallback="'unknown'")
        report_url_expr = col_expr("report_url", "Report_URL", fallback="''")
        attachment_url_expr = col_expr("attachment_url", "Attachment_URL", fallback="''")
        storage_path_expr = col_expr("storage_path", "Storage_Path", fallback="''")
        download_url_expr = col_expr("download_url", "Download_URL", fallback="''")
        order_col = pick_col(
            "updated_at",
            "Updated_At",
            "created_at",
            "Created_At",
            "id",
            "ID",
        )
        order_clause = f'ORDER BY "{order_col}" DESC' if order_col else ""

        match_source_expr = f"COALESCE({ingredient_name_expr}, {normalized_names_expr}, {ingredient_id_expr}, '')"
        ingredient_match_expr = (
            f"regexp_replace(lower({match_source_expr}), '[^a-z0-9]+', '', 'g')"
        )

        where_clauses = [f"COALESCE({download_url_expr}, '') <> ''"]
        params: list[Any] = []
        if requested_names:
            where_clauses.append(f"{ingredient_match_expr} = ANY(%s)")
            params.append(requested_names)

        cur.execute(
            f'''
            SELECT
                COALESCE({ingredient_id_expr}, {normalized_names_expr}, {pdf_name_expr}, '') AS ingredient_id,
                COALESCE({ingredient_name_expr}, {pdf_name_expr}, 'unknown') AS cir_ingredient_name,
                COALESCE({function_expr}, 'unknown') AS function,
                COALESCE({report_url_expr}, '') AS report_url,
                COALESCE({attachment_url_expr}, '') AS attachment_url,
                COALESCE({storage_path_expr}, '') AS storage_path,
                COALESCE({download_url_expr}, '') AS download_url,
                COALESCE({pdf_name_expr}, '') AS pdf_name,
                COALESCE({normalized_names_expr}, '') AS normalized_names
            FROM "{table_name}"
            WHERE {' AND '.join(where_clauses)}
            {order_clause}
            {'' if requested_names else 'LIMIT %s'}
            ''',
            tuple(params if requested_names else params + [limit]),
        )
        rows = cur.fetchall()

        def _row_matches_requested(row: Any, requested: set[str]) -> str | None:
            ingredient_id_val = str(row[0] or "")
            ingredient_name_val = str(row[1] or "")
            normalized_names_val = str(row[8] or "")

            normalized_candidates = {
                _normalize_lookup_name(ingredient_id_val),
                _normalize_lookup_name(ingredient_name_val),
            }
            if normalized_names_val:
                normalized_candidates.update(
                    _normalize_lookup_name(part)
                    for part in re.split(r"[|,;\\s]+", normalized_names_val)
                    if part.strip()
                )
                normalized_candidates.add(_normalize_lookup_name(normalized_names_val))

            for req in requested:
                if req in normalized_candidates:
                    return req
                if any(req in cand or cand in req for cand in normalized_candidates if cand):
                    return req
            return None

        records: list[dict[str, str]] = []
        if requested_names:
            remaining = set(requested_names)
            matched_records: dict[str, dict[str, str]] = {}
            for row in rows:
                matched_name = _row_matches_requested(row, remaining)
                if not matched_name:
                    continue

                matched_records[matched_name] = {
                    "ingredient_id": str(row[0] or ""),
                    "cir_ingredient_name": str(row[1] or "unknown"),
                    "function": str(row[2] or "unknown"),
                    "report_url": str(row[3] or ""),
                    "attachment_url": str(row[4] or ""),
                    "storage_path": str(row[5] or ""),
                    "download_url": str(row[6] or ""),
                    "pdf_name": str(row[7] or ""),
                }
                remaining.discard(matched_name)
                if not remaining:
                    break

            records = [matched_records[name] for name in requested_names if name in matched_records]
        else:
            for row in rows:
                records.append(
                    {
                        "ingredient_id": str(row[0] or ""),
                        "cir_ingredient_name": str(row[1] or "unknown"),
                        "function": str(row[2] or "unknown"),
                        "report_url": str(row[3] or ""),
                        "attachment_url": str(row[4] or ""),
                        "storage_path": str(row[5] or ""),
                        "download_url": str(row[6] or ""),
                        "pdf_name": str(row[7] or ""),
                    }
                )
        return records
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    """
    Smoke test:
    1) Pull ingredient records from Postgres (with non-empty download_url)
    2) For each, download PDF from Firebase + chunk/classify + upsert
    3) Run one cross-ingredient safety query

    Required env vars:
        POSTGRESS_PASSWORD

    Optional env vars:
        SMOKE_TEST_TABLE (default: cir_pdfs)
        SMOKE_TEST_INGREDIENTS (comma-separated names)
        SMOKE_TEST_LIMIT (default: len(SMOKE_TEST_INGREDIENTS or default list))
        DEMO_QUERY
    """
    default_smoke_test_ingredients = [
        "Ascorbic acid",
        "Retinol"
    ]

    ingredient_list_env = os.environ.get("SMOKE_TEST_INGREDIENTS", "").strip()
    smoke_test_ingredients = (
        [item.strip() for item in ingredient_list_env.split(",") if item.strip()]
        if ingredient_list_env
        else default_smoke_test_ingredients
    )

    smoke_test_table = os.environ.get("SMOKE_TEST_TABLE", "cir_pdfs")
    smoke_test_limit = int(
        os.environ.get("SMOKE_TEST_LIMIT", str(len(smoke_test_ingredients)))
    )
    demo_query = os.environ.get(
        "DEMO_QUERY",
        "What does this say about safety.",
    )

    logger.info(
        "Starting VectorStore lazy-index smoke test | table=%s | limit=%s | ingredients=%s",
        smoke_test_table,
        smoke_test_limit,
        smoke_test_ingredients,
    )

    records = _fetch_smoke_test_records(
        limit=smoke_test_limit,
        table_name=smoke_test_table,
        ingredient_names=smoke_test_ingredients,
    )
    if not records:
        raise ValueError(
            "No smoke-test records found for requested ingredients with non-empty download_url"
        )

    logger.info("Fetched %s smoke-test records from Postgres", len(records))

    requested_lookup = [_normalize_lookup_name(name) for name in smoke_test_ingredients]
    fetched_lookup = {
        _normalize_lookup_name(rec.get("cir_ingredient_name", ""))
        or _normalize_lookup_name(rec.get("ingredient_id", ""))
        for rec in records
    }
    missing = [name for name, norm in zip(smoke_test_ingredients, requested_lookup) if norm not in fetched_lookup]
    if missing:
        raise ValueError(f"Missing smoke-test ingredients in fetched records: {missing}")

    store = VectorStore()

    ingredient_ids: list[str] = []
    total_inserted = 0
    for i, rec in enumerate(records, start=1):
        ingredient_id = rec["ingredient_id"] or f"smoke-{i}"
        ingredient_name = rec["cir_ingredient_name"] or f"Ingredient {i}"
        pdf_url = rec["download_url"]

        logger.info(
            "[%s/%s] Indexing ingredient_id=%s name=%s",
            i,
            len(records),
            ingredient_id,
            ingredient_name,
        )

        summary = store.index_pdf_from_firebase(
            pdf_url=pdf_url,
            ingredient_id=ingredient_id,
            cir_ingredient_name=ingredient_name,
            function=rec["function"] or "unknown",
            report_url=rec["report_url"],
            attachment_url=rec["attachment_url"] or pdf_url,
            storage_path=rec["storage_path"] or pdf_url,
        )
        total_inserted += int(summary.get("inserted_vector_count", 0))
        ingredient_ids.append(ingredient_id)
        logger.info("[%s/%s] Indexing summary: %s", i, len(records), summary)

    unique_ingredient_ids = list(dict.fromkeys(ingredient_ids))
    logger.info(
        "Completed smoke indexing for %s ingredients | total_vectors=%s",
        len(unique_ingredient_ids),
        total_inserted,
    )

    matches = store.query_safety_report(
        query_text=demo_query,
        ingredient_ids=unique_ingredient_ids,
        top_k=15,
        score_threshold=0.1
    )

    logger.info("Smoke query returned %s matches", len(matches))
    for i, match in enumerate(matches, start=1):
        metadata = getattr(match, "metadata", {}) or {}
        logger.info(
            "Match %s | score=%.4f | ingredient=%s | page=%s",
            i,
            float(getattr(match, "score", 0.0)),
            metadata.get("cir_ingredient_name", ""),
            metadata.get("page", ""),
        )
        print("-----------------text-----------------------", end="\n\n")
        print(metadata.get("chunk_text"))

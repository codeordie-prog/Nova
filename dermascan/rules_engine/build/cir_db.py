"""
sqlite helper utilities for CIR PDF ingestion
"""

import sqlite3
from typing import List, Dict, Any
import os
from dataclasses import dataclass


DB_PATH = r"C:\Users\LENOVO\Documents\cir\cir-crawler\data\cir_pdfs.db"


@dataclass(slots=True)
class CIRPDFRow:
    """
    Normalized CIR row used by the ingestion pipeline.

    Attributes:
        cir_ingredient_id (str): Ingredient identifier used for filtering in vector search.
        cir_ingredient_name (str): Human-readable ingredient name.
        function (str): Ingredient function from the CIR database.
        report_url (str): CIR report URL.
        attachment_view_url (str): Public attachment URL.
        pdf_path (str): Absolute filesystem path to the source PDF.
    """

    cir_ingredient_id: str
    cir_ingredient_name: str
    function: str
    report_url: str
    attachment_view_url: str
    pdf_path: str


def _connect_db(db_path: str = DB_PATH):
    """Create and return SQLite connection"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # allows column access by name
    return conn


def _get_tables(conn: sqlite3.Connection) -> List[str]:
    """Return list of tables in the database"""
    cursor = conn.cursor()

    cursor.execute("""
        SELECT name 
        FROM sqlite_master 
        WHERE type='table'
    """)

    return [row["name"] for row in cursor.fetchall()]


def _extract_schema(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """
    Extract schema for a given table
    Returns list of columns with metadata
    """
    cursor = conn.cursor()

    cursor.execute(f"PRAGMA table_info('{table_name}')")

    schema = []
    for row in cursor.fetchall():
        schema.append({
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default_value": row[4],
            "primary_key": bool(row[5])
        })

    return schema


def _get_all_schemas(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Get schema for all tables in DB
    """
    tables = _get_tables(conn)

    return {
        table: _extract_schema(conn, table)
        for table in tables
    }


def _fetch_sample_rows(
    conn: sqlite3.Connection,
    table_name: str,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """
    Fetch sample rows for debugging ingestion
    """
    cursor = conn.cursor()

    cursor.execute(f"SELECT * FROM {table_name} LIMIT {limit}")

    rows = cursor.fetchall()

    return [dict(row) for row in rows]


def _close(conn: sqlite3.Connection):
    """Close DB connection safely"""
    conn.close()


def _fetch_cir_pdf_rows(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> list[CIRPDFRow]:
    """
    Fetch CIR PDF ingestion rows with only the metadata fields required by vector ingestion.

    Args:
        conn (sqlite3.Connection): Open SQLite connection.
        limit (int | None): Optional upper bound for returned rows.

    Returns:
        list[CIRPDFRow]: Normalized rows ready for document loading and chunking.
    """
    query = """
        SELECT
            cir_ingredient_id,
            cir_ingredient_name,
            Function,
            report_url,
            attachment_view_url,
            pdf_path
        FROM cir_pdfs
        WHERE
            pdf_path IS NOT NULL
            AND TRIM(pdf_path) <> ''
            AND cir_ingredient_id IS NOT NULL
            AND TRIM(cir_ingredient_id) <> ''
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += "\nLIMIT ?"
        params = (limit,)

    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()

    normalized: list[CIRPDFRow] = []
    for row in rows:
        normalized.append(
            CIRPDFRow(
                cir_ingredient_id=str(row["cir_ingredient_id"] or "").strip(),
                cir_ingredient_name=str(row["cir_ingredient_name"] or "").strip(),
                function=str(row["Function"] or "").strip(),
                report_url=str(row["report_url"] or "").strip(),
                attachment_view_url=str(row["attachment_view_url"] or "").strip(),
                pdf_path=str(row["pdf_path"] or "").strip(),
            )
        )

    return normalized


if __name__ == "__main__":

    conn = _connect_db()
    schema = _get_all_schemas(conn)

    column_names = {
        table: [col["name"] for col in columns]
        for table, columns in schema.items()
    }

    print(column_names)

    _close(conn)
"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.


Migration script from local sqlite to postgres
"""


import os
import re
import sqlite3
import sys

import psycopg2


DB_PATH = r"C:\Users\LENOVO\Documents\cir\cir-crawler\data\cir_pdfs.db"

PASSWORD = os.environ.get("postgres_password", "Kelvinjoe692@")  # (note: don't hardcode in production)
HOST = "localhost"
PORT = "5432"
DATABASE = "postgres"
USER = "postgres"

CLOUD_SQL_HOST = os.environ.get("CLOUD_POSTGRES_HOST", "34.79.111.107")
CLOUD_SQL_PORT = os.environ.get("CLOUD_POSTGRES_PORT", "5432")
CLOUD_SQL_DATABASE = os.environ.get("CLOUD_POSTGRES_DATABASE", "qutis_db")
CLOUD_SQL_USER = os.environ.get("CLOUD_POSTGRES_USER", "postgres")
CLOUD_SQL_SSLMODE = os.environ.get("CLOUD_POSTGRES_SSLMODE", "require")
CLOUD_SQL_PASSWORD = "Dermascan@a1122"

# -----------------------------
# SQLITE CONNECTION
# -----------------------------
def _establish_sqlite_connection(path: str = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _get_tables(conn: sqlite3.Connection) -> list:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    ).fetchall()

    # skip sqlite internal tables
    return [t[0] for t in tables if not t[0].startswith("sqlite_")]


def _get_table_schema(conn: sqlite3.Connection, table_name: str) -> list:
    return conn.execute(f"PRAGMA table_info({table_name});").fetchall()


# -----------------------------
# SCHEMA GENERATOR
# -----------------------------
def generate_postgres_create_table(table_name: str, schema: list[tuple]) -> str:

    type_map = {
        "INTEGER": "INTEGER",
        "TEXT": "TEXT",
        "TIMESTAMP": "TIMESTAMP",
    }

    columns = []
    primary_keys = []

    # check if pdf_path exists in schema
    has_pdf_path = any(col[1].lower() == "pdf_path" for col in schema)
    has_cir_ingredient_name = any(col[1].lower() == "cir_ingredient_name" for col in schema)

    for cid, name, col_type, notnull, default, pk in schema:

        pg_type = type_map.get(col_type.upper(), "TEXT")

        # Handle primary key upgrade
        if pk and name.lower() == "id":
            col_def = f'"{name}" BIGSERIAL PRIMARY KEY'
        else:
            col_def = f'"{name}" {pg_type}'

        # NOT NULL
        if notnull:
            col_def += " NOT NULL"

        # DEFAULT handling
        if default is not None:

            default_str = str(default).strip()

            if default_str.upper() in ["CURRENT_TIMESTAMP", "NOW()"]:
                col_def += " DEFAULT CURRENT_TIMESTAMP"

            else:
                # strip existing quotes
                if (default_str.startswith("'") and default_str.endswith("'")) or \
                   (default_str.startswith('"') and default_str.endswith('"')):
                    default_str = default_str[1:-1]

                default_str = default_str.replace("'", "''")

                if pg_type == "TEXT":
                    col_def += f" DEFAULT '{default_str}'"
                else:
                    col_def += f" DEFAULT {default_str}"

        columns.append(col_def)

    # add pdf_name column if pdf_path exists
    if has_pdf_path:
        columns.append('"pdf_name" TEXT')

    # add normalized_names column if cir_ingredient_name exists
    if has_cir_ingredient_name:
        columns.append('"normalized_names" TEXT')

    sql = (
        f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    '
        + ",\n    ".join(columns)
        + "\n);"
    )

    return sql


# -----------------------------
# POSTGRES CONNECTION
# -----------------------------
def _establish_postgres_conn():
    return psycopg2.connect(
        host=HOST,
        port=PORT,
        database=DATABASE,
        user=USER,
        password=PASSWORD
    )


def _create_table_postgres(cmd_str: str):
    conn = _establish_postgres_conn()
    cur = conn.cursor()

    try:
        cur.execute(cmd_str)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[ERROR creating table] {e}")
        raise
    finally:
        cur.close()
        conn.close()


def _add_normalized_names_column_if_missing(table_name: str):
    """
    Add normalized_names column to existing table if it doesn't exist.
    """
    conn = _establish_postgres_conn()
    cur = conn.cursor()

    try:
        # Check if normalized_names column exists
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = 'normalized_names';
        """, (table_name,))
        exists = cur.fetchone()

        if not exists:
            # Only add normalized_names if cir_ingredient_name exists
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'cir_ingredient_name';
            """, (table_name,))
            has_name_column = cur.fetchone()

            if has_name_column:
                cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "normalized_names" TEXT;')
                conn.commit()
                print(f"  Added normalized_names column to {table_name}")
            else:
                print(f"  Skipping normalized_names column for {table_name} (no cir_ingredient_name column)")
        else:
            print(f"  normalized_names column already exists in {table_name}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR adding normalized_names column] {e}")
        raise
    finally:
        cur.close()
        conn.close()


def _add_pdf_name_column_if_missing(table_name: str):
    """
    Add pdf_name column to existing table if it doesn't exist.
    """
    conn = _establish_postgres_conn()
    cur = conn.cursor()

    try:
        # Check if pdf_name column exists
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = 'pdf_name';
        """, (table_name,))
        exists = cur.fetchone()

        if not exists:
            # Check if pdf_path column exists first
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'pdf_path';
            """, (table_name,))
            has_pdf_path = cur.fetchone()

            if has_pdf_path:
                cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "pdf_name" TEXT;')
                conn.commit()
                print(f"  Added pdf_name column to {table_name}")
            else:
                print(f"  Skipping pdf_name column for {table_name} (no pdf_path column)")
        else:
            print(f"  pdf_name column already exists in {table_name}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR adding pdf_name column] {e}")
        raise
    finally:
        cur.close()
        conn.close()


def _update_pdf_name_from_pdf_path(table_name: str):
    """
    Update existing rows: extract pdf_name from pdf_path for rows where pdf_name is NULL or empty.
    """
    conn = _establish_postgres_conn()
    cur = conn.cursor()

    try:
        # Check if both columns exist
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name IN ('pdf_path', 'pdf_name');
        """, (table_name,))
        columns = [row[0] for row in cur.fetchall()]

        if "pdf_path" not in columns or "pdf_name" not in columns:
            print(f"  Skipping pdf_name update for {table_name} (missing columns)")
            return

        # Update rows where pdf_name is NULL or empty
        cur.execute(f'''
            UPDATE "{table_name}"
            SET "pdf_name" = SUBSTRING("pdf_path" FROM (LENGTH("pdf_path") - POSITION('/' IN REVERSE("pdf_path")) + 2))
            WHERE "pdf_name" IS NULL OR "pdf_name" = '';
        ''')
        updated = cur.rowcount
        conn.commit()
        print(f"  Updated {updated} rows with pdf_name in {table_name}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR updating pdf_name] {e}")
        raise
    finally:
        cur.close()
        conn.close()


def _update_normalized_names_from_ingredient_name(table_name: str):
    """
    Update existing rows: normalize cir_ingredient_name into normalized_names.
    Rule: lowercase + keep only alphanumeric characters.
    """
    conn = _establish_postgres_conn()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name IN ('cir_ingredient_name', 'normalized_names');
        """, (table_name,))
        columns = [row[0] for row in cur.fetchall()]

        if "cir_ingredient_name" not in columns or "normalized_names" not in columns:
            print(f"  Skipping normalized_names update for {table_name} (missing columns)")
            return

        cur.execute(f'''
            UPDATE "{table_name}"
            SET "normalized_names" = regexp_replace(LOWER("cir_ingredient_name"), '[^a-z0-9]+', '', 'g')
            WHERE
                ("normalized_names" IS NULL OR "normalized_names" = '')
                AND "cir_ingredient_name" IS NOT NULL
                AND "cir_ingredient_name" <> '';
        ''')
        updated = cur.rowcount
        conn.commit()
        print(f"  Updated {updated} rows with normalized_names in {table_name}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR updating normalized_names] {e}")
        raise
    finally:
        cur.close()
        conn.close()


# -----------------------------
# DATA MIGRATION
# -----------------------------
def _migrate_data(table_name: str):
    import os

    sqlite_conn = _establish_sqlite_connection()
    pg_conn = _establish_postgres_conn()

    try:
        sqlite_cur = sqlite_conn.cursor()

        # get column info (excluding id)
        sqlite_cur.execute(f"PRAGMA table_info({table_name})")
        columns_info = sqlite_cur.fetchall()

        base_columns = [col[1] for col in columns_info if col[1].lower() != "id"]
        columns = list(base_columns)

        # check if pdf_path column exists and add pdf_name
        has_pdf_path = "pdf_path" in columns
        if has_pdf_path:
            columns.append("pdf_name")

        # check if cir_ingredient_name exists and add normalized_names
        has_cir_ingredient_name = "cir_ingredient_name" in columns
        if has_cir_ingredient_name:
            columns.append("normalized_names")

        if not columns:
            print(f"No columns to migrate for {table_name}")
            return

        # SELECT only needed columns
        select_columns = ", ".join(
            [f'"{c}"' for c in base_columns]
        )
        sqlite_cur.execute(f"SELECT {select_columns} FROM {table_name}")
        rows = sqlite_cur.fetchall()

        if not rows:
            print(f"  No data to migrate for table: {table_name}")
            return

        # process rows: extract pdf_name if pdf_path exists
        processed_rows = []
        for row in rows:
            row_list = list(row)

            if has_pdf_path:
                pdf_path_idx = base_columns.index("pdf_path")
                pdf_path = row_list[pdf_path_idx]
                pdf_name = os.path.basename(pdf_path) if pdf_path else ""
                row_list.append(pdf_name)

            if has_cir_ingredient_name:
                name_idx = base_columns.index("cir_ingredient_name")
                ingredient_name = row_list[name_idx]
                normalized_name = re.sub(r"[^a-z0-9]+", "", ingredient_name.lower()) if ingredient_name else ""
                row_list.append(normalized_name)

            processed_rows.append(tuple(row_list))

        pg_cur = pg_conn.cursor()

        placeholders = ", ".join(["%s"] * len(columns))
        columns_str = ", ".join([f'"{col}"' for col in columns])

        insert_sql = f'''
            INSERT INTO "{table_name}" ({columns_str})
            VALUES ({placeholders})
        '''

        pg_cur.executemany(insert_sql, processed_rows)
        pg_conn.commit()

        print(f"  Migrated {len(rows)} rows into {table_name}")

    except Exception as e:
        pg_conn.rollback()
        print(f"[ERROR migrating {table_name}] {e}")
        raise

    finally:
        sqlite_conn.close()
        pg_conn.close()


# -----------------------------
# CLOUD SQL CONNECTION TEST
# -----------------------------
def _cloud_sql_password() -> str:
    """
    Resolve the Cloud SQL password from environment variables or module config.

    Returns:
        str: Postgres password for the Cloud SQL instance.

    Raises:
        ValueError: If no password is configured.
    """
    password = (
        os.environ.get("CLOUD_POSTGRES_PASSWORD")
        #or os.environ.get("POSTGRESS_PASSWORD")
        #or os.environ.get("postgres_password")
        or CLOUD_SQL_PASSWORD
    )
    if not password:
        raise ValueError(
            "Cloud SQL password is required. Set CLOUD_POSTGRES_PASSWORD or CLOUD_SQL_PASSWORD."
        )
    return password


def _establish_cloud_sql_conn():
    """
    Open a psycopg2 connection to the GCP Cloud SQL instance.

    Returns:
        psycopg2 connection configured with SSL.

    Raises:
        ValueError: If the password is missing.
        psycopg2.Error: If the connection fails.
    """
    return psycopg2.connect(
        host=CLOUD_SQL_HOST,
        port=CLOUD_SQL_PORT,
        database=CLOUD_SQL_DATABASE,
        user=CLOUD_SQL_USER,
        password=_cloud_sql_password(),
        sslmode=CLOUD_SQL_SSLMODE,
    )


def test_cloud_sql_connection(*, table_name: str = "cir_pdfs") -> bool:
    """
    Verify connectivity to the GCP Cloud SQL database.

    Runs a version check and, when present, reports row count for the target table.

    Args:
        table_name: Table to probe after the connection succeeds.

    Returns:
        bool: True when the connection and probe queries succeed.

    Raises:
        ValueError: If the password is missing.
        psycopg2.Error: If connection or probe queries fail.
    """
    conn = _establish_cloud_sql_conn()
    cur = conn.cursor()

    try:
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        print(f"Connected to Cloud SQL at {CLOUD_SQL_HOST}:{CLOUD_SQL_PORT}/{CLOUD_SQL_DATABASE}")
        print(f"  sslmode={CLOUD_SQL_SSLMODE}")
        print(f"  postgres version: {version}")

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            );
            """,
            (table_name,),
        )
        table_exists = cur.fetchone()[0]

        if table_exists:
            cur.execute(f'SELECT COUNT(*) FROM "{table_name}";')
            row_count = cur.fetchone()[0]
            print(f"  table {table_name!r}: {row_count} rows")
        else:
            print(f"  table {table_name!r}: not found (connection still OK)")

        print("Cloud SQL connection test passed.")
        return True

    finally:
        cur.close()
        conn.close()


# -----------------------------
# MAIN PIPELINE
# -----------------------------
if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "test-cloud-sql":
        test_cloud_sql_connection()
        sys.exit(0)

    sqlite_conn = _establish_sqlite_connection()
    tables = _get_tables(sqlite_conn)

    print("Tables to migrate:", tables)

    
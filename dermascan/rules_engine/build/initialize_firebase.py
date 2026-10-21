"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly."""


import firebase_admin
from firebase_admin import App, credentials, firestore, storage
from firebase_admin.firestore import Client
import os
from urllib.parse import quote

import psycopg2

from dermascan.shared.logger import get_logger
from dermascan.shared.env_paths import resolve_env_path


PDFS_PATH = r"C:/Users/LENOVO/Documents/cir/cir-crawler/data/pdfs"
DEFAULT_STORAGE_PREFIX = "cir-pdfs"
POSTGRES_HOST = os.environ.get("postgres_host", "localhost")
POSTGRES_PORT = os.environ.get("postgres_port", "5432")
POSTGRES_DATABASE = os.environ.get("postgres_database", "postgres")
POSTGRES_USER = os.environ.get("postgres_user", "postgres")
POSTGRES_PASSWORD = os.environ.get("postgres_password", "Kelvinjoe692@")

logger = get_logger(__name__)


def _establish_postgres_conn():
    if not POSTGRES_PASSWORD:
        raise ValueError("postgres_password environment variable is required for Postgres sync")

    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        database=POSTGRES_DATABASE,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def _ensure_storage_columns(table_name: str) -> None:
    conn = _establish_postgres_conn()
    cur = conn.cursor()
    try:
        cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "storage_bucket" TEXT;')
        cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "storage_path" TEXT;')
        cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "download_url" TEXT;')
        cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "storage_updated_at" TIMESTAMP;')
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _backfill_missing_pdf_names(table_name: str) -> int:
    """
    Fill missing pdf_name values from pdf_path using SQL-safe path normalization.
    """
    conn = _establish_postgres_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f'''
            UPDATE "{table_name}"
            SET "pdf_name" = regexp_replace(replace("pdf_path", E'\\\\', '/'), '^.*/', '')
            WHERE
                ("pdf_name" IS NULL OR "pdf_name" = '')
                AND "pdf_path" IS NOT NULL
                AND "pdf_path" <> ''
            '''
        )
        updated_rows = cur.rowcount
        conn.commit()
        return updated_rows
    finally:
        cur.close()
        conn.close()


def _update_postgres_storage_metadata(
    *,
    table_name: str,
    pdf_name: str,
    storage_bucket: str,
    storage_path: str,
    download_url: str,
) -> int:
    conn = _establish_postgres_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f'''
            UPDATE "{table_name}"
            SET
                "storage_bucket" = %s,
                "storage_path" = %s,
                "download_url" = %s,
                "storage_updated_at" = NOW()
            WHERE
                COALESCE("pdf_name", '') = %s
                AND COALESCE("pdf_path", '') <> ''
            ''',
            (storage_bucket, storage_path, download_url, pdf_name),
        )
        updated_rows = cur.rowcount
        conn.commit()
        return updated_rows
    finally:
        cur.close()
        conn.close()


def _resolve_cred_path() -> str | None:
    config_path = resolve_env_path(os.environ.get("CONFIG_FILE_PATH", ""))
    if config_path and os.path.exists(config_path):
        return config_path
    return None


def initialize_firebase_app() -> App:
    """
    Initialize Firebase Admin SDK app.
    """
    if firebase_admin._apps:
        return firebase_admin.get_app()

    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", "dawagpn.firebasestorage.app")
    options: dict[str, str] = {}
    if bucket_name:
        options["storageBucket"] = bucket_name

    try:
        app = firebase_admin.initialize_app(options=options)
        logger.info("Firebase initialized with Application Default Credentials")
        return app
    except Exception as e:
        logger.warning(f"Could not initialize with Application Default Credentials: {e}")
        logger.info("Trying service account JSON file...")

        cred_path = _resolve_cred_path()
        if not cred_path:
            raise FileNotFoundError("Firebase credentials file not found in any of the expected locations")

        try:
            cred = credentials.Certificate(cred_path)
            app = firebase_admin.initialize_app(cred, options=options)
            logger.info(f"Firebase initialized with service account from {cred_path}")
            return app
        except Exception as e:
            logger.error(f"Failed to initialize Firebase with service account: {e}")
            raise Exception(f"Failed to initialize Firebase with service account: {e}")


def initialize_firebase() -> Client:
    """
    Initialize Firebase and return a Firestore client.
    """
    initialize_firebase_app()
    return firestore.client()


def get_storage_bucket() -> storage.bucket:
    """
    Initialize Firebase and return Storage bucket client.
    """
    app = initialize_firebase_app()
    bucket = storage.bucket(app=app)
    if not bucket.name:
        raise ValueError(
            "Storage bucket is not configured. Set FIREBASE_STORAGE_BUCKET env var, "
            "for example: your-project-id.appspot.com"
        )
    return bucket


def upload_pdfs_to_firebase_storage(
    *,
    local_pdfs_path: str = PDFS_PATH,
    storage_prefix: str = DEFAULT_STORAGE_PREFIX,
    skip_existing: bool = True,
    sync_to_postgres: bool = True,
    postgres_table: str = "cir_pdfs",
) -> dict[str, int]:
    """
    Upload local PDFs to Firebase Storage.

    Files are uploaded under: <storage_prefix>/<filename>.pdf
    """
    if not os.path.isdir(local_pdfs_path):
        raise FileNotFoundError(f"Local PDF folder not found: {local_pdfs_path}")

    bucket = get_storage_bucket()
    file_names = [
        name
        for name in os.listdir(local_pdfs_path)
        if os.path.isfile(os.path.join(local_pdfs_path, name)) and name.lower().endswith(".pdf")
    ]
    file_names = sorted(dict.fromkeys(file_names))

    summary = {
        "total_local_pdfs": len(file_names),
        "uploaded": 0,
        "skipped_existing": 0,
        "skipped_duplicate_local": 0,
        "failed": 0,
        "postgres_rows_updated": 0,
    }

    seen_remote_paths: set[str] = set()

    if sync_to_postgres:
        _ensure_storage_columns(postgres_table)
        backfilled = _backfill_missing_pdf_names(postgres_table)
        logger.info("Backfilled missing pdf_name rows in %s: %s", postgres_table, backfilled)

    logger.info(
        "Starting Firebase Storage upload | bucket=%s | local_dir=%s | pdf_count=%s",
        bucket.name,
        local_pdfs_path,
        len(file_names),
    )

    for idx, filename in enumerate(file_names, start=1):
        local_path = os.path.join(local_pdfs_path, filename)
        remote_path = f"{storage_prefix}/{filename}" if storage_prefix else filename

        if remote_path in seen_remote_paths:
            summary["skipped_duplicate_local"] += 1
            logger.info("[%s/%s] Skipped duplicate local filename: %s", idx, len(file_names), remote_path)
            continue
        seen_remote_paths.add(remote_path)

        blob = bucket.blob(remote_path)
        storage_uri = f"gs://{bucket.name}/{remote_path}"
        object_url = f"https://storage.googleapis.com/{bucket.name}/{quote(remote_path)}"

        try:
            if skip_existing and blob.exists():
                summary["skipped_existing"] += 1
                logger.info("[%s/%s] Skipped existing: %s", idx, len(file_names), remote_path)
                if sync_to_postgres:
                    updated = _update_postgres_storage_metadata(
                        table_name=postgres_table,
                        pdf_name=filename,
                        storage_bucket=bucket.name,
                        storage_path=storage_uri,
                        download_url=object_url,
                    )
                    summary["postgres_rows_updated"] += updated
                continue

            blob.upload_from_filename(local_path, content_type="application/pdf")
            summary["uploaded"] += 1
            logger.info("[%s/%s] Uploaded: %s", idx, len(file_names), remote_path)
            if sync_to_postgres:
                updated = _update_postgres_storage_metadata(
                    table_name=postgres_table,
                    pdf_name=filename,
                    storage_bucket=bucket.name,
                    storage_path=storage_uri,
                    download_url=object_url,
                )
                summary["postgres_rows_updated"] += updated
        except Exception as exc:
            summary["failed"] += 1
            logger.exception("[%s/%s] Upload failed for %s", idx, len(file_names), local_path)
            logger.error("Reason: %s", exc)

    logger.info("Firebase Storage upload summary: %s", summary)
    return summary


if __name__ == "__main__":
    report = upload_pdfs_to_firebase_storage()
    print(report)


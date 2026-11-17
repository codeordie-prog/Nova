"""
Author: Dermascan Team
Date: 2026-05-19
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: This module contains reusable functions for Firebase.
"""

import os
from dotenv import load_dotenv
from logging import Logger
from urllib.parse import unquote, urlparse

from firebase_admin import App, storage

from dermascan.rules_engine.firebase_init.init_firebase import CREDENTIALS_PATH, _init_firebase_app
from dermascan.shared.env_paths import resolve_env_path
from dermascan.shared.logger import get_logger

# Load env variables
load_dotenv()

# Initialize logger
logger: Logger = get_logger(__name__)


def _get_app() -> App:
    if not CREDENTIALS_PATH:
        raise ValueError("CONFIG_FILE_PATH is required and cannot be empty")
    resolved_credentials_path = resolve_env_path(CREDENTIALS_PATH, required=True)
    return _init_firebase_app(resolved_credentials_path)


def _get_storage_bucket(app: App):
    """
    Get the storage bucket from the Firebase app.
    
    Args:
        app: Firebase app instance
        
    Returns:
        Storage bucket instance
    """
    try:
        bucket = storage.bucket(app=app)
        if not bucket or not bucket.name:
            raise ValueError(
            "Storage bucket is not configured. Set FIREBASE_STORAGE_BUCKET env var, "
            "for example: your-project-id.appspot.com"
        )
        return bucket

    except ValueError:
        logger.error("Storage bucket is not configured. Set FIREBASE_STORAGE_BUCKET env var,")
        raise

    except Exception as e:
        logger.exception("An error occured while getting the storage bucket: %s", e)
        raise


def _extract_blob_path_from_url(url: str) -> str:
    parsed = urlparse(url)

    if url.startswith("gs://"):
        # gs://bucket/path/to/file.pdf
        path = url.replace("gs://", "", 1)
        parts = path.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid gs url: {url}")
        return parts[1]

    # Firebase style: .../o/<encoded_path>?...
    if "/o/" in parsed.path:
        encoded_path = parsed.path.split("/o/", 1)[1]
        if "/" in encoded_path:
            encoded_path = encoded_path.split("/", 1)[0]
        decoded_path = unquote(encoded_path)
        if not decoded_path:
            raise ValueError(f"Could not parse object path from url: {url}")
        return decoded_path

    # Google storage style: /bucket/path/to/file.pdf
    raw_path = parsed.path.lstrip("/")
    if parsed.netloc in {"storage.googleapis.com", "firebasestorage.googleapis.com"}:
        parts = raw_path.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Could not parse object path from url: {url}")
        return parts[1]

    raise ValueError(f"Unsupported Firebase/Google storage URL format: {url}")


def _download_pdf_from_url(url: str, output_dir: str = "downloads") -> str:
    """
    Download a PDF from a Firebase/Google Storage URL into local output_dir.

    Returns:
        str: local downloaded file path
    """
    bucket = _get_storage_bucket(_get_app())
    blob_path = _extract_blob_path_from_url(url)
    filename = os.path.basename(blob_path)
    if not filename.lower().endswith(".pdf"):
        raise ValueError(f"Blob is not a PDF: {blob_path}")

    os.makedirs(output_dir, exist_ok=True)
    local_file_path = os.path.join(output_dir, filename)

    blob = bucket.blob(blob_path)
    if not blob.exists():
        raise FileNotFoundError(f"Blob not found in bucket: {blob_path}")

    blob.download_to_filename(local_file_path)
    logger.info("Downloaded PDF from %s to %s", blob_path, local_file_path)
    return local_file_path


def _upload_pdf_to_bucket(
    pdf_path: str,
    *,
    destination_prefix: str = "cir-pdfs",
    skip_existing: bool = True,
) -> dict[str, str | bool]:
    """
    Upload a local PDF to Firebase Storage.

    Returns:
        dict[str, str | bool]: upload metadata
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF path does not exist: {pdf_path}")
    if not pdf_path.lower().endswith(".pdf"):
        raise ValueError(f"Only PDF files are supported: {pdf_path}")

    bucket = _get_storage_bucket(_get_app())
    filename = os.path.basename(pdf_path)
    remote_path = f"{destination_prefix}/{filename}" if destination_prefix else filename

    blob = bucket.blob(remote_path)
    if skip_existing and blob.exists():
        logger.info("Skipped existing blob: %s", remote_path)
        return {
            "uploaded": False,
            "bucket": bucket.name,
            "blob_path": remote_path,
            "gs_uri": f"gs://{bucket.name}/{remote_path}",
        }

    blob.upload_from_filename(pdf_path, content_type="application/pdf")
    logger.info("Uploaded PDF %s to %s", pdf_path, remote_path)
    return {
        "uploaded": True,
        "bucket": bucket.name,
        "blob_path": remote_path,
        "gs_uri": f"gs://{bucket.name}/{remote_path}",
    }


if __name__ == "__main__":
    dir_path = "C:\\Users\\LENOVO\\Documents\\dermascanAgents\\dermascan\\rules_engine\\firebase_init\\downloads"
    url = "https://storage.googleapis.com/dawagpn.firebasestorage.app/cir-pdfs/c9cc1a17-8e74-ec11-8943-0022482f06a6.pdf"
    _download_pdf_from_url(url=url, output_dir=dir_path)
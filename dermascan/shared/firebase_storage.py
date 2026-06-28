"""
Author: Dermascan Team
Date: 2026-05-23
Licence: MIT

Description: Firebase Storage helpers for shared use across backend and services.

Client chat images are uploaded under: client-media/{client_id}/{uuid}.{ext}

NOTE: This module uses signed URLs to download images from Firebase Storage.
NOTE: SIGNED URLS ARE ONLY VALID FOR 24 HOURS.

"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from urllib.parse import unquote, urlparse

import firebase_admin
from firebase_admin import App, credentials, storage

from dermascan.shared.logger import get_logger
from dermascan.shared.env_paths import resolve_env_path

# Initialize Logger
logger = get_logger(__name__)

# Set client's media prefix
CLIENT_MEDIA_PREFIX = "client-media"
_SIGNED_URL_TTL_S: int = int(os.getenv("WORKERS_JOB_TTL_S", "86400"))


def _resolve_credentials_path() -> str | None:
    """
    Resolve the credentials path from the environment variable or fallback paths.

    Returns:
        The credentials path if found, otherwise None.
    
    Raises:
        FileNotFoundError: If the credentials file is not found.
    """
    config_path = resolve_env_path(os.environ.get("CONFIG_FILE_PATH", ""))
    if config_path and os.path.exists(config_path):
        return config_path

    repo_relative_candidates = [
        "dermascan/backend/services/agents/configurations/dawagpn-1c9b72f24ea8.json",
        "configs/dawagpn-1c9b72f24ea8.json",
    ]
    for candidate in repo_relative_candidates:
        resolved = resolve_env_path(candidate)
        if resolved and os.path.exists(resolved):
            return resolved
    return None


def initialize_firebase_app() -> App:
    """
    Initialize Firebase Admin SDK app (idempotent).

    Returns:
        The initialized Firebase app.
    
    Raises:
        ValueError: If the FIREBASE_STORAGE_BUCKET is not set.
        FileNotFoundError: If the credentials file is not found.
    """
    if firebase_admin._apps:
        return firebase_admin.get_app()

    bucket_name = os.environ.get("FIREBASE_STORAGE_BUCKET", "").strip()
    if not bucket_name:
        raise ValueError(
            "FIREBASE_STORAGE_BUCKET is required. "
            "Set it to your Firebase Storage bucket name."
        )

    options: dict[str, str] = {"storageBucket": bucket_name}
    cred_path = _resolve_credentials_path()
    if cred_path:
        try:
            cred = credentials.Certificate(cred_path)
            app = firebase_admin.initialize_app(cred, options=options)
            logger.info("Firebase initialized with service account from %s", cred_path)
            return app
        except Exception:
            logger.exception("Could not initialize Firebase with service account: %s", cred_path)
            raise

    try:
        app = firebase_admin.initialize_app(options=options)
        logger.info("Firebase initialized with Application Default Credentials")
        return app
    except Exception as exc:
        logger.warning("Could not initialize with Application Default Credentials: %s", exc)
        raise FileNotFoundError(
            "Firebase credentials file not found. Set CONFIG_FILE_PATH or place "
            "credentials under configs/."
        ) from exc


def get_storage_bucket() -> storage.bucket:
    """
    Return the configured Firebase Storage bucket.

    Returns:
        The configured Firebase Storage bucket.
    
    Raises:
        ValueError: If the storage bucket is not configured.
    """
    app = initialize_firebase_app()
    bucket = storage.bucket(app=app)
    if not bucket.name:
        raise ValueError(
            "Storage bucket is not configured. Set FIREBASE_STORAGE_BUCKET."
        )
    return bucket


def _extension_for_mime(mime_type: str) -> str:
    """
    Determine the file extension for a given MIME type.

    Args:
        mime_type: The MIME type to determine the extension for.

    Returns:
        The file extension for the given MIME type.
    """
    lowered = (mime_type or "").strip().lower()
    if lowered == "image/png":
        return ".png"
    if lowered == "image/gif":
        return ".gif"
    if lowered == "image/webp":
        return ".webp"
    return ".jpg"


def extract_blob_path_from_url(url: str) -> str:
    """
    Extract the object path inside the bucket from a Firebase/GCS HTTPS URL.

    Args:
        url: The URL to extract the blob path from

    Returns:
        The blob path
    
    Raises:
        ValueError: If the URL is not a valid Firebase/GCS HTTPS URL
    """
    parsed = urlparse(url)

    if url.startswith("gs://"):
        path = url.replace("gs://", "", 1)
        parts = path.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid gs url: {url}")
        return parts[1]

    if "/o/" in parsed.path:
        encoded_path = parsed.path.split("/o/", 1)[1]
        if "/" in encoded_path:
            encoded_path = encoded_path.split("/", 1)[0]
        decoded_path = unquote(encoded_path)
        if not decoded_path:
            raise ValueError(f"Could not parse object path from url: {url}")
        return decoded_path

    raw_path = parsed.path.lstrip("/")
    if parsed.netloc in {"storage.googleapis.com", "firebasestorage.googleapis.com"}:
        parts = raw_path.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Could not parse object path from url: {url}")
        return parts[1]

    raise ValueError(f"Unsupported Firebase/Google storage URL format: {url}")


def download_client_image_bytes_by_path(blob_path: str) -> bytes:
    """
    Download a client image by durable Firebase blob path (no signed URL).

    Args:
        blob_path: Object path inside the configured bucket.

    Returns:
        The image bytes.

    Raises:
        FileNotFoundError: If the blob is not found.
        ValueError: If blob_path is empty.
    """
    normalized = str(blob_path or "").strip()
    if not normalized:
        raise ValueError("blob_path is required")
    # Defense-in-depth: only ever serve client media, never an arbitrary bucket path.
    if not normalized.startswith(f"{CLIENT_MEDIA_PREFIX}/"):
        raise ValueError("blob_path outside client media prefix")
    bucket = get_storage_bucket()
    blob = bucket.blob(normalized)
    if not blob.exists():
        raise FileNotFoundError(f"Blob not found: {normalized}")
    return blob.download_as_bytes()


def download_client_image_bytes(*, url: str) -> bytes:
    """
    Download a client image using Firebase Admin credentials (private buckets).

    Args:
        url: The URL to download the image from

    Returns:
        The image bytes
    
    Raises:
        FileNotFoundError: If the blob is not found
        ValueError: If the URL is not a valid Firebase/GCS HTTPS URL
    """
    blob_path = extract_blob_path_from_url(url)
    bucket = get_storage_bucket()
    blob = bucket.blob(blob_path)
    if not blob.exists():
        raise FileNotFoundError(f"Blob not found: {blob_path}")
    return blob.download_as_bytes()


def upload_client_image(
    *,
    client_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> str:
    """
    Upload a client image to Firebase Storage and return a signed HTTPS URL.

    Objects are stored at: client-media/{client_id}/{uuid}.{ext}
    A new UUID is used for every upload.

    Args:
        client_id: End-user identifier used to partition storage paths.
        image_bytes: Raw image bytes (caller should validate before upload).
        mime_type: Image MIME type (e.g. image/jpeg).

    Returns:
        Signed download URL for the uploaded object (valid for WORKERS_JOB_TTL_S).
    """
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id:
        raise ValueError("client_id is required for client image upload")

    if not image_bytes:
        raise ValueError("image_bytes must not be empty")

    ext = _extension_for_mime(mime_type)
    object_name = f"{uuid.uuid4().hex}{ext}"
    remote_path = f"{CLIENT_MEDIA_PREFIX}/{normalized_client_id}/{object_name}"

    bucket = get_storage_bucket()
    blob = bucket.blob(remote_path)
    blob.upload_from_string(image_bytes, content_type=mime_type or "image/jpeg")

    download_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=_SIGNED_URL_TTL_S),
        method="GET",
    )
    logger.info(
        "Uploaded client image: client_id=%s path=%s bytes=%s signed_ttl_s=%s",
        normalized_client_id,
        remote_path,
        len(image_bytes),
        _SIGNED_URL_TTL_S,
    )
    return download_url

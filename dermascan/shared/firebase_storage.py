"""
Author: Dermascan Team
Date: 2026-05-23
Licence: MIT
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: Firebase Storage helpers for shared use across backend and services.

Client chat images are uploaded under: client-media/{client_id}/{uuid}.{ext}

NOTE: This module uses signed URLs to download images from Firebase Storage.
NOTE: SIGNED URLS ARE ONLY VALID FOR 24 HOURS.
"""


from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ClientImageUpload:
    """
    Result of uploading one client image to Firebase Storage.

    Attributes:
        blob_path: Durable object path under ``client-media/``.
        signed_url: Ephemeral HTTPS URL valid for ``WORKERS_JOB_TTL_S``.
    """

    blob_path: str
    signed_url: str


def _resolve_credentials_path() -> str | None:
    """
    Resolve the service-account credentials path from CONFIG_FILE_PATH.

    Returns:
        The resolved path if CONFIG_FILE_PATH is set and the file exists,
        otherwise None (the caller falls back to Application Default
        Credentials). No credential filenames are hardcoded.
    """
    config_path = resolve_env_path(os.environ.get("CONFIG_FILE_PATH", ""))
    if config_path and os.path.exists(config_path):
        return config_path
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
        except ValueError:
            # Lost a race with another thread that initialized the default app
            # between the guard above and here; reuse the existing app.
            if firebase_admin._apps:
                return firebase_admin.get_app()
            logger.exception("Could not initialize Firebase with service account: %s", cred_path)
            raise
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
        return unquote(parts[1])

    raise ValueError(f"Unsupported Firebase/Google storage URL format: {url}")


def normalize_image_blob_path(raw: str) -> str:
    """
    Normalize an image reference to a durable ``client-media/...`` blob path.

    Args:
        raw: ``client-media/...`` path or HTTPS Firebase/GCS URL.

    Returns:
        Normalized blob path, or empty string when not resolvable.
    """
    normalized = str(raw or "").strip()
    if not normalized:
        return ""
    if normalized.startswith(f"{CLIENT_MEDIA_PREFIX}/"):
        return normalized
    if normalized.lower().startswith(("http://", "https://")):
        try:
            return extract_blob_path_from_url(normalized)
        except ValueError:
            return ""
    return ""


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


def sign_client_image_blob_path(blob_path: str) -> str:
    """
    Generate a fresh signed HTTPS URL for a durable client-media blob path.

    Args:
        blob_path: Object path inside the configured bucket (``client-media/...``).

    Returns:
        Signed download URL valid for ``WORKERS_JOB_TTL_S`` seconds.

    Raises:
        ValueError: If ``blob_path`` is empty or outside the client-media prefix.
        FileNotFoundError: If the blob does not exist in the bucket.
    """
    normalized = str(blob_path or "").strip()
    if not normalized:
        raise ValueError("blob_path is required")
    if not normalized.startswith(f"{CLIENT_MEDIA_PREFIX}/"):
        raise ValueError("blob_path outside client media prefix")

    bucket = get_storage_bucket()
    blob = bucket.blob(normalized)
    if not blob.exists():
        raise FileNotFoundError(f"Blob not found: {normalized}")

    signed_url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=_SIGNED_URL_TTL_S),
        method="GET",
    )
    logger.debug(
        "Signed client image URL: path=%s ttl_s=%s",
        normalized,
        _SIGNED_URL_TTL_S,
    )
    return signed_url


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
) -> ClientImageUpload:
    """
    Upload a client image to Firebase Storage.

    Objects are stored at: client-media/{client_id}/{uuid}.{ext}
    A new UUID is used for every upload.

    Args:
        client_id: End-user identifier used to partition storage paths.
        image_bytes: Raw image bytes (caller should validate before upload).
        mime_type: Image MIME type (e.g. image/jpeg).

    Returns:
        ClientImageUpload with durable ``blob_path`` and ephemeral ``signed_url``.
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
    return ClientImageUpload(blob_path=remote_path, signed_url=download_url)

"""
Author: Dermascan Team
Date: 2026-05-19
Licence: PolyForm Noncommercial License 1.0.0
Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Description: This module is an implementation of firebase client initialization.
"""


import os
from dotenv import load_dotenv
from logging import Logger
from typing import Dict

import firebase_admin
from firebase_admin import App, credentials, firestore
from firebase_admin.firestore import Client

from dermascan.shared.logger import get_logger
from dermascan.shared.env_paths import resolve_env_path

# Load env variables
load_dotenv()

# initialize logger
logger: Logger = get_logger(__name__)

CREDENTIALS_PATH: str = str(os.environ.get("CONFIG_FILE_PATH", ""))
STORAGE_BUCKET_NAME: str = str(os.environ.get("FIREBASE_STORAGE_BUCKET", ""))


def _init_firebase_app(credentials_path: str) -> App:
    """
    Initialize Firebase SDK app

    Args:
        credentials_path (str): Path to the credentials file
    
    Returns:
        App: Firebase app instance
    
    Raises:
        FileNotFoundError: If the credentials file is not found
        ValueError: If the FIREBASE_STORAGE_BUCKET is not set
        Exception: If there is an error initializing the Firebase app
    
    """
    # Check if already initialized
    if firebase_admin._apps:
        return firebase_admin.get_app()

    try:
        logger.info("Initializing firebase app ....")
        options: Dict[str, str] = {}
        bucket_name: str = STORAGE_BUCKET_NAME

        if not bucket_name:
            logger.error("FIREBASE_STORAGE_BUCKET is not set.")
            raise ValueError("FIREBASE_STORAGE_BUCKET field cannot be empty.")

        options["storageBucket"] = bucket_name

        if not credentials_path:
            raise ValueError("Firebase credentials path is empty")

        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Credentials file not found in the specified path : {credentials_path}")
        cred = credentials.Certificate(credentials_path)

        app = firebase_admin.initialize_app(
            credential=cred,
            options=options
        )

        logger.info(f"FIREBASE APP initialized with credentials: {credentials_path}")

        return app

    except FileNotFoundError:
        logger.error(f"Credentials file not found in the specified path : {credentials_path}")
        raise
    except ValueError:
        logger.error("An exception occured during firebase app initialization, check if the FIREBASE_STORAGE_BUCKET is in your environment variables. ")
        raise
    except Exception as e:
        logger.exception("Failed to initialize Firebase App: %s", e)
        raise


def _initialize_firebase() -> Client:
    """
    Initialize Firebase and return a Firestore client.
    """
    if not CREDENTIALS_PATH:
        raise ValueError("CONFIG_FILE_PATH is required and cannot be empty")

    resolved_credentials_path = resolve_env_path(CREDENTIALS_PATH, required=True)
    _init_firebase_app(resolved_credentials_path)
    logger.info("Successfully initialized Firebase.")
    return firestore.client()


if __name__ == "__main__":
    _initialize_firebase()
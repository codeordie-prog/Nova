#!/usr/bin/env python3
"""
Upload local ``.env`` (+ credential JSON files) to GCP Secret Manager for staging.

Run once before ``cloudbuild.staging.yaml`` (or whenever ``.env`` changes):

    python deploy/scripts/sync_staging_secrets.py --project qutisai

Requires: gcloud CLI authenticated, Secret Manager API enabled.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


SECRET_PREFIX = "dermascan"
ENV_SUFFIX = "dotenv-staging"
GOOGLE_CREDS_SECRET = f"{SECRET_PREFIX}-google-credentials-staging"
FIREBASE_CONFIG_SECRET = f"{SECRET_PREFIX}-firebase-config-staging"
RUN_SA_NAME = "dermascan-cloud-run"

# Cloud-specific overrides applied to the uploaded .env (paths + container layout).
CLOUD_ENV_OVERRIDES: dict[str, str] = {
    "DERMASCAN_ROOT": "/app",
    "GOOGLE_APPLICATION_CREDENTIALS_QUTIS": "/secrets/google/credentials.json",
    "CONFIG_FILE_PATH": "/secrets/firebase/config.json",
    "TESSERACT_PATH": "/usr/bin/tesseract",
    "GOOGLE_GENAI_USE_VERTEXAI": "true",
}

# Set at deploy time by deploy-staging-services.sh — omit stale local URLs from sync.
DEPLOY_TIME_KEYS = frozenset({"MEDGEMMA_GATEWAY_URL", "TELEGRAM_LOCAL_WEBHOOK_URL"})


def _repo_root() -> Path:
    cursor = Path(__file__).resolve().parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not find repo root (pyproject.toml)")


def _parse_dotenv(path: Path) -> dict[str, str]:
    """
    Parse a dotenv file into a key/value dict.

    Args:
        path: Path to ``.env``.

    Returns:
        dict[str, str]: Parsed variables (no quotes stripped for multiline safety).
    """
    variables: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        variables[key] = value
    return variables


def _gcloud_argv() -> list[str]:
    """
    Resolve the gcloud CLI executable (Windows needs ``gcloud.cmd``).

    Returns:
        list[str]: argv prefix for gcloud subprocess calls.

    Raises:
        RuntimeError: When gcloud is not installed or not on PATH.
    """
    for candidate in ("gcloud", "gcloud.cmd"):
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved]
    raise RuntimeError(
        "gcloud CLI not found in PATH. Install Google Cloud SDK and run "
        "'gcloud auth login'."
    )


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> None:
    """
    Run a subprocess and raise on failure.

    Args:
        cmd: Command argv.
        input_bytes: Optional stdin payload.

    Raises:
        RuntimeError: When the command exits non-zero.
    """
    result = subprocess.run(
        cmd,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        stdout = result.stdout.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n{stdout}\n{stderr}"
        )


def _secret_exists(project: str, secret_id: str) -> bool:
    result = subprocess.run(
        _gcloud_argv() + ["secrets", "describe", secret_id, f"--project={project}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _upsert_secret(project: str, secret_id: str, payload: bytes) -> None:
    """
    Create or add a new version for a Secret Manager secret.

    Args:
        project: GCP project id.
        secret_id: Secret name.
        payload: Secret bytes.
    """
    if _secret_exists(project, secret_id):
        _run(
            _gcloud_argv()
            + ["secrets", "versions", "add", secret_id, f"--project={project}", "--data-file=-"],
            input_bytes=payload,
        )
        print(f"updated secret: {secret_id}")
        return
    _run(
        _gcloud_argv()
        + ["secrets", "create", secret_id, f"--project={project}", "--data-file=-"],
        input_bytes=payload,
    )
    print(f"created secret: {secret_id}")


def _grant_accessor(project: str, secret_id: str, member: str) -> None:
    _run(
        _gcloud_argv()
        + [
            "secrets",
            "add-iam-policy-binding",
            secret_id,
            f"--project={project}",
            f"--member={member}",
            "--role=roles/secretmanager.secretAccessor",
        ]
    )


def _resolve_repo_path(repo_root: Path, raw_path: str) -> Path:
    path = Path(raw_path.strip().strip('"').strip("'"))
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def _build_cloud_dotenv(repo_root: Path, env_path: Path, project_id: str) -> bytes:
    """
    Build cloud-ready dotenv bytes from local ``.env``.

    Args:
        repo_root: Repository root.
        env_path: Local ``.env`` path.
        project_id: GCP project id for project-scoped vars.

    Returns:
        bytes: UTF-8 dotenv file content for Secret Manager.
    """
    variables = _parse_dotenv(env_path)
    for key, value in CLOUD_ENV_OVERRIDES.items():
        variables[key] = value
    variables["GOOGLE_CLOUD_PROJECT_QUTIS"] = project_id
    for key in DEPLOY_TIME_KEYS:
        variables.pop(key, None)

    lines: list[str] = []
    for key in sorted(variables.keys()):
        value = variables[key]
        if value == "":
            continue
        escaped = value.replace("\n", "\\n")
        lines.append(f"{key}={escaped}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    """
    CLI entrypoint.

    Returns:
        int: Process exit code.
    """
    parser = argparse.ArgumentParser(description="Sync .env to GCP Secret Manager (staging)")
    parser.add_argument("--project", default="qutisai", help="GCP project id")
    parser.add_argument("--env-file", default="", help="Path to .env (default: repo root .env)")
    args = parser.parse_args()

    project: str = args.project.strip()
    repo_root = _repo_root()
    env_path = Path(args.env_file).resolve() if args.env_file else repo_root / ".env"
    if not env_path.is_file():
        print(f"ERROR: .env not found at {env_path}", file=sys.stderr)
        return 1

    variables = _parse_dotenv(env_path)
    run_sa = f"serviceAccount:{RUN_SA_NAME}@{project}.iam.gserviceaccount.com"

    dotenv_bytes = _build_cloud_dotenv(repo_root, env_path, project)
    dotenv_secret = f"{SECRET_PREFIX}-{ENV_SUFFIX}"
    _upsert_secret(project, dotenv_secret, dotenv_bytes)
    _grant_accessor(project, dotenv_secret, run_sa)

    google_path_raw = variables.get("GOOGLE_APPLICATION_CREDENTIALS_QUTIS", "")
    if google_path_raw:
        google_path = _resolve_repo_path(repo_root, google_path_raw)
        if not google_path.is_file():
            print(f"ERROR: Google credentials not found: {google_path}", file=sys.stderr)
            return 1
        _upsert_secret(project, GOOGLE_CREDS_SECRET, google_path.read_bytes())
        _grant_accessor(project, GOOGLE_CREDS_SECRET, run_sa)

    firebase_path_raw = variables.get("CONFIG_FILE_PATH", "")
    if firebase_path_raw:
        firebase_path = _resolve_repo_path(repo_root, firebase_path_raw)
        if not firebase_path.is_file():
            print(f"ERROR: Firebase config not found: {firebase_path}", file=sys.stderr)
            return 1
        _upsert_secret(project, FIREBASE_CONFIG_SECRET, firebase_path.read_bytes())
        _grant_accessor(project, FIREBASE_CONFIG_SECRET, run_sa)

    print("")
    print("Staging secrets synced.")
    print(f"  dotenv:   {dotenv_secret}")
    print(f"  google:   {GOOGLE_CREDS_SECRET}")
    print(f"  firebase: {FIREBASE_CONFIG_SECRET}")
    print("")
    print("Next:")
    print(
        "  gcloud builds submit --config=deploy/cloudbuild/cloudbuild.staging.yaml "
        f"--substitutions=_REGION=europe-west1,_TAG=latest,_POSTGRES_HOST=<your-postgres-ip>"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Helpers for live-stack end-to-end pipeline probes.

Connects to the running backend API, Redis, and (implicitly) workers started
outside pytest. Used by ``dermascan/tests/e2e/`` smoke, latency, and
concurrency suites.
"""

from __future__ import annotations

import csv
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import redis
import requests

from dermascan.backend.services.agents.reclaim_logic import processed_key
from dermascan.tests.API.probe_helpers import percentile_ms
from dermascan.tests.fixtures.payloads import whatsapp_image_webhook, whatsapp_text_webhook

IMAGES_DIR: Path = Path(__file__).resolve().parent.parent / "images"
_SUPPORTED_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
E2E_RESULTS_CSV: Path = Path(
    os.getenv(
        "DERMASCAN_E2E_RESULTS_CSV",
        Path(__file__).resolve().parent / "e2e_results.csv",
    )
)

E2E_CSV_HEADERS: tuple[str, ...] = (
    "timestamp",
    "suite",
    "test_name",
    "status",
    "concurrency",
    "job_count",
    "success_count",
    "success_rate",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "min_ms",
    "max_ms",
    "avg_ms",
    "sample_reply",
    "notes",
)


@dataclass(frozen=True)
class E2EConfig:
    """Runtime configuration for live-stack E2E probes."""

    base_url: str
    redis_url: str
    ingress_secret: str
    job_stream: str
    outbound_stream: str
    http_timeout_s: float
    pipeline_timeout_s: float
    poll_interval_s: float


@dataclass(frozen=True)
class WebhookResult:
    """Result of a WhatsApp ingress POST."""

    status_code: int
    body: dict[str, Any]
    latency_s: float
    request_ids: list[str]


@dataclass(frozen=True)
class PipelineCompletion:
    """Observed completion of one enqueued orchestrator job."""

    request_id: str
    elapsed_s: float
    job_message_id: str | None
    saw_outbound: bool
    marked_processed: bool


@dataclass(frozen=True)
class OrchestratorReply:
    """Orchestrator outbound reply captured from ``outbound:messages``."""

    request_id: str
    elapsed_s: float
    reply_text: str
    kind: str
    client_id: str


@dataclass(frozen=True)
class E2EResultRow:
    """One persisted row in ``e2e_results.csv``."""

    suite: str
    test_name: str
    status: str
    concurrency: int
    job_count: int
    success_count: int
    success_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    avg_ms: float
    sample_reply: str
    notes: str


def load_e2e_config() -> E2EConfig:
    """Load E2E probe settings from environment variables.

    Returns:
        Parsed E2E configuration with production-aligned defaults.
    """
    base_url = os.getenv(
        "DERMASCAN_API_URL",
        os.getenv("DERMASCAN_GATEWAY_URL", "http://localhost:8088"),
    ).rstrip("/")
    redis_url = os.getenv("REDIS_URL", "").strip()
    ingress_secret = os.getenv(
        "WHATSAPP_INGRESS_SECRET",
        os.getenv("INTERNAL_API_SECRET", ""),
    ).strip()
    return E2EConfig(
        base_url=base_url,
        redis_url=redis_url,
        ingress_secret=ingress_secret,
        job_stream=os.getenv("ORCHESTRATOR_JOB_STREAM", "orchestrator:jobs"),
        outbound_stream=os.getenv("OUTBOUND_STREAM", "outbound:messages"),
        http_timeout_s=float(os.getenv("DERMASCAN_HTTP_TIMEOUT", "120")),
        pipeline_timeout_s=float(os.getenv("DERMASCAN_E2E_PIPELINE_TIMEOUT_S", "180")),
        poll_interval_s=float(os.getenv("DERMASCAN_E2E_POLL_INTERVAL_S", "0.5")),
    )


def ensure_e2e_stack_ready(cfg: E2EConfig) -> None:
    """Skip the current test when the live stack is unreachable.

    Args:
        cfg: E2E configuration.

    Raises:
        pytest.skip: When backend or Redis is not available.
    """
    if not cfg.redis_url:
        pytest.skip("REDIS_URL is not set — start Redis and configure .env")

    health_url = f"{cfg.base_url}/health"
    try:
        response = requests.get(health_url, timeout=5)
    except requests.RequestException as exc:
        pytest.skip(f"Backend API not reachable at {health_url} ({exc})")
    if response.status_code >= 500:
        pytest.skip(f"Backend API unhealthy at {health_url}, status {response.status_code}")

    try:
        client = redis.Redis.from_url(cfg.redis_url, decode_responses=True)
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis not reachable at REDIS_URL ({exc})")


def build_redis_client(cfg: E2EConfig) -> redis.Redis:
    """Create a decode-responses Redis client for stream inspection.

    Args:
        cfg: E2E configuration.

    Returns:
        Connected Redis client.
    """
    return redis.Redis.from_url(cfg.redis_url, decode_responses=True)


def load_sample_image_bytes() -> list[bytes]:
    """Load real image samples from ``dermascan/tests/images/``.

    Accepts JPEG, PNG, and WebP (formats supported by backend upload validation).

    Returns:
        Non-empty list of image byte payloads.

    Raises:
        pytest.skip: When the images directory is missing or has no supported files.
    """
    if not IMAGES_DIR.exists():
        pytest.skip(f"Sample images dir missing: {IMAGES_DIR}")

    images: list[bytes] = []
    for suffix in _SUPPORTED_IMAGE_SUFFIXES:
        for path in sorted(IMAGES_DIR.glob(f"*{suffix}")):
            images.append(path.read_bytes())

    if images:
        return images

    unsupported = sorted(
        path.name for path in IMAGES_DIR.iterdir() if path.is_file() and path.suffix.lower() == ".avif"
    )
    hint = ""
    if unsupported:
        hint = (
            f" Found .avif files ({', '.join(unsupported[:3])}) but upload validation "
            "only accepts JPEG/PNG/WebP/GIF — add a .jpg sample."
        )
    pytest.skip(f"No supported images in {IMAGES_DIR} (need .jpg/.jpeg/.png/.webp).{hint}")


def upload_test_media(
    cfg: E2EConfig,
    image_bytes: bytes,
    *,
    filename: str = "e2e-sample.jpg",
) -> str:
    """Upload one image via ``POST /v1/media/test/upload``.

    Args:
        cfg: E2E configuration.
        image_bytes: Raw JPEG bytes.
        filename: Multipart filename.

    Returns:
        Redis ``media_ref`` key for webhook attachment references.

    Raises:
        requests.HTTPError: When upload fails.
    """
    url = f"{cfg.base_url}/v1/media/test/upload"
    response = requests.post(
        url,
        files={"image": (filename, image_bytes, "image/jpeg")},
        timeout=cfg.http_timeout_s,
    )
    response.raise_for_status()
    media_ref = str(response.json().get("media_ref") or "").strip()
    if not media_ref:
        raise RuntimeError("Upload response missing media_ref")
    return media_ref


def post_whatsapp_webhook(
    cfg: E2EConfig,
    payload: dict[str, Any],
) -> WebhookResult:
    """POST a WhatsApp webhook envelope to the backend ingress.

    Args:
        cfg: E2E configuration.
        payload: WhatsApp webhook JSON body.

    Returns:
        HTTP status, parsed JSON body, latency, and any queued request IDs.

    Raises:
        pytest.skip: When ingress secret is not configured.
    """
    if not cfg.ingress_secret:
        pytest.skip("WHATSAPP_INGRESS_SECRET or INTERNAL_API_SECRET required for E2E webhooks")

    url = f"{cfg.base_url}/v1/webhooks/whatsapp"
    headers = {
        "X-Internal-Secret": cfg.ingress_secret,
        "Content-Type": "application/json",
    }
    start = time.perf_counter()
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=cfg.http_timeout_s,
    )
    elapsed = time.perf_counter() - start
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    request_ids = [str(rid) for rid in body.get("request_ids") or []]
    return WebhookResult(
        status_code=response.status_code,
        body=body,
        latency_s=elapsed,
        request_ids=request_ids,
    )


def enqueue_text_job(
    cfg: E2EConfig,
    *,
    message_id: str | None = None,
    from_id: str | None = None,
    text: str | None = None,
) -> WebhookResult:
    """Enqueue one text-only WhatsApp job.

    Args:
        cfg: E2E configuration.
        message_id: Optional stable provider message id.
        from_id: Optional WhatsApp sender id.
        text: Optional message body.

    Returns:
        Webhook ingress result.
    """
    payload = whatsapp_text_webhook(
        message_id=message_id or f"wamid.e2e.{uuid.uuid4().hex}",
        from_id=from_id or f"2547{uuid.uuid4().int % 10_000_000_000:010d}",
        text=text or "E2E text probe — please reply briefly.",
    )
    return post_whatsapp_webhook(cfg, payload)


def enqueue_image_job(
    cfg: E2EConfig,
    image_bytes: bytes,
    *,
    message_id: str | None = None,
    from_id: str | None = None,
    text: str | None = None,
) -> WebhookResult:
    """Upload an image and enqueue one WhatsApp job referencing it.

    Args:
        cfg: E2E configuration.
        image_bytes: JPEG bytes to upload.
        message_id: Optional stable provider message id.
        from_id: Optional WhatsApp sender id.
        text: Optional caption / prompt text.

    Returns:
        Webhook ingress result after media upload.
    """
    media_ref = upload_test_media(cfg, image_bytes)
    payload = whatsapp_image_webhook(
        message_id=message_id or f"wamid.e2e.img.{uuid.uuid4().hex}",
        from_id=from_id or f"2547{uuid.uuid4().int % 10_000_000_000:010d}",
        media_ref=media_ref,
        text=text or "E2E image probe — please diagnose this.",
    )
    return post_whatsapp_webhook(cfg, payload)


def find_job_message_id(
    redis_client: redis.Redis,
    *,
    stream: str,
    request_id: str,
    scan_count: int = 300,
) -> str | None:
    """Find the stream entry id for a queued orchestrator job.

    Args:
        redis_client: Connected Redis client.
        stream: Orchestrator job stream name.
        request_id: Request id returned by ingress.
        scan_count: Maximum recent entries to scan.

    Returns:
        Redis stream message id, or None if not found yet.
    """
    entries = redis_client.xrevrange(stream, count=scan_count)
    for msg_id, fields in entries:
        if str(fields.get("request_id") or "") == request_id:
            return str(msg_id)
    return None


def find_outbound_fields(
    redis_client: redis.Redis,
    *,
    stream: str,
    request_id: str,
    scan_count: int = 500,
) -> tuple[str | None, dict[str, str]]:
    """Locate outbound stream fields for a request id.

    Args:
        redis_client: Connected Redis client.
        stream: Outbound message stream name.
        request_id: Orchestrator request id to locate.
        scan_count: Maximum recent entries to scan.

    Returns:
        Tuple of stream message id and field mapping, or ``(None, {})`` if missing.
    """
    entries = redis_client.xrevrange(stream, count=scan_count)
    for msg_id, fields in entries:
        if str(fields.get("request_id") or "") == request_id:
            return str(msg_id), {str(key): str(value) for key, value in fields.items()}
    return None, {}


def outbound_contains_request_id(
    redis_client: redis.Redis,
    *,
    stream: str,
    request_id: str,
    scan_count: int = 300,
) -> bool:
    """Return True when outbound stream contains the given request id.

    Args:
        redis_client: Connected Redis client.
        stream: Outbound message stream name.
        request_id: Orchestrator request id to locate.
        scan_count: Maximum recent entries to scan.

    Returns:
        Whether a matching outbound entry exists.
    """
    entries = redis_client.xrevrange(stream, count=scan_count)
    for _, fields in entries:
        if str(fields.get("request_id") or "") == request_id:
            return True
    return False


def wait_for_pipeline_completion(
    redis_client: redis.Redis,
    cfg: E2EConfig,
    request_id: str,
    *,
    timeout_s: float | None = None,
) -> PipelineCompletion:
    """Wait until a queued job is processed or an outbound reply appears.

    Success is defined as either:
    - processed marker set on the orchestrator job entry, or
    - an outbound stream entry referencing the same request id.

    Args:
        redis_client: Connected Redis client.
        cfg: E2E configuration.
        request_id: Request id returned by ingress.
        timeout_s: Optional override for pipeline timeout.

    Returns:
        Completion metadata including elapsed seconds.

    Raises:
        TimeoutError: When neither signal appears before the deadline.
    """
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else cfg.pipeline_timeout_s)
    start = time.monotonic()
    job_message_id: str | None = None
    saw_outbound = False
    marked_processed = False

    while time.monotonic() < deadline:
        if job_message_id is None:
            job_message_id = find_job_message_id(
                redis_client,
                stream=cfg.job_stream,
                request_id=request_id,
            )

        if job_message_id and redis_client.exists(processed_key(cfg.job_stream, job_message_id)):
            marked_processed = True
            break

        if outbound_contains_request_id(
            redis_client,
            stream=cfg.outbound_stream,
            request_id=request_id,
        ):
            saw_outbound = True
            break

        time.sleep(cfg.poll_interval_s)

    elapsed = time.monotonic() - start
    if not marked_processed and not saw_outbound:
        raise TimeoutError(
            f"Pipeline did not complete for request_id={request_id} within "
            f"{timeout_s or cfg.pipeline_timeout_s:.0f}s "
            f"(job_msg_id={job_message_id})"
        )

    return PipelineCompletion(
        request_id=request_id,
        elapsed_s=elapsed,
        job_message_id=job_message_id,
        saw_outbound=saw_outbound,
        marked_processed=marked_processed,
    )


def wait_for_orchestrator_reply(
    redis_client: redis.Redis,
    cfg: E2EConfig,
    request_id: str,
    *,
    timeout_s: float | None = None,
) -> OrchestratorReply:
    """Wait until orchestrator enqueues a non-empty outbound reply for a job.

    Polls ``outbound:messages`` for an entry with matching ``request_id`` and
    non-empty ``text``. This validates the orchestrator produced a user-facing
    response, not merely that ingress returned 200.

    Args:
        redis_client: Connected Redis client.
        cfg: E2E configuration.
        request_id: Request id returned by ingress.
        timeout_s: Optional override for pipeline timeout.

    Returns:
        Captured outbound reply metadata.

    Raises:
        TimeoutError: When no outbound reply appears before the deadline.
    """
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else cfg.pipeline_timeout_s)
    start = time.monotonic()

    while time.monotonic() < deadline:
        _, fields = find_outbound_fields(
            redis_client,
            stream=cfg.outbound_stream,
            request_id=request_id,
        )
        if fields:
            reply_text = str(fields.get("text") or "").strip()
            if reply_text:
                return OrchestratorReply(
                    request_id=request_id,
                    elapsed_s=time.monotonic() - start,
                    reply_text=reply_text,
                    kind=str(fields.get("kind") or ""),
                    client_id=str(fields.get("client_id") or ""),
                )
        time.sleep(cfg.poll_interval_s)

    raise TimeoutError(
        f"No orchestrator outbound reply for request_id={request_id} within "
        f"{timeout_s or cfg.pipeline_timeout_s:.0f}s"
    )


def load_e2e_concurrency_levels(default_levels: list[int] | None = None) -> list[int]:
    """Load parallel orchestrator test concurrency levels.

    Args:
        default_levels: Fallback when env/config unset.

    Returns:
        Sorted unique concurrency levels.
    """
    fallback = default_levels or [20, 40]
    raw = os.getenv("DERMASCAN_E2E_CONCURRENCY_LEVELS", "").strip()
    if raw:
        levels = [int(part.strip()) for part in raw.split(",") if part.strip()]
    else:
        levels = fallback
    return sorted({level for level in levels if level >= 1})


def assert_webhook_queued(result: WebhookResult) -> str:
    """Assert ingress accepted the job and return the sole request id.

    Args:
        result: Webhook POST result.

    Returns:
        Queued request id.

    Raises:
        AssertionError: When ingress did not queue exactly one job.
    """
    assert result.status_code == 200, f"Unexpected ingress status {result.status_code}: {result.body}"
    assert result.body.get("status") == "queued", result.body
    assert len(result.request_ids) == 1, result.body
    return result.request_ids[0]


def summarize_latencies_ms(latencies_s: list[float]) -> dict[str, float]:
    """Compute p50/p95/p99 latency percentiles in milliseconds.

    Args:
        latencies_s: Observed durations in seconds.

    Returns:
        Percentile mapping.
    """
    return {
        "p50_ms": percentile_ms(latencies_s, 50),
        "p95_ms": percentile_ms(latencies_s, 95),
        "p99_ms": percentile_ms(latencies_s, 99),
    }


def e2e_results_csv_path() -> Path:
    """Return the path where E2E probe results are appended.

    Returns:
        CSV file path (override with ``DERMASCAN_E2E_RESULTS_CSV``).
    """
    return E2E_RESULTS_CSV


def write_e2e_result(row: E2EResultRow, *, output_path: Path | None = None) -> Path:
    """Append one E2E result row to the results CSV.

    Creates the file and header row when missing.

    Args:
        row: Result metrics to persist.
        output_path: Optional override path (defaults to :data:`E2E_RESULTS_CSV`).

    Returns:
        Path written to.
    """
    target = output_path or e2e_results_csv_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_header = not target.exists() or target.stat().st_size == 0

    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(E2E_CSV_HEADERS)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            row.suite,
            row.test_name,
            row.status,
            row.concurrency,
            row.job_count,
            row.success_count,
            f"{row.success_rate:.4f}",
            f"{row.p50_ms:.2f}",
            f"{row.p95_ms:.2f}",
            f"{row.p99_ms:.2f}",
            f"{row.min_ms:.2f}",
            f"{row.max_ms:.2f}",
            f"{row.avg_ms:.2f}",
            row.sample_reply,
            row.notes,
        ])

    return target


def record_e2e_result(
    *,
    suite: str,
    test_name: str,
    latencies_s: list[float],
    concurrency: int,
    success_count: int,
    job_count: int,
    sample_reply: str = "",
    notes: str = "",
    status: str = "passed",
) -> Path:
    """Compute latency stats and append one row to ``e2e_results.csv``.

    Args:
        suite: High-level suite name (smoke, latency, orchestrator, concurrency).
        test_name: Pytest node name or descriptive label.
        latencies_s: Observed durations in seconds (one per job).
        concurrency: Parallel worker count for the run.
        success_count: Jobs that completed successfully.
        job_count: Total jobs attempted.
        sample_reply: Truncated orchestrator reply for spot checks.
        notes: Free-form context (SLO flags, config overrides, etc.).
        status: Result label (typically ``passed`` or ``failed``).

    Returns:
        Path to the CSV file updated.
    """
    if not latencies_s:
        latencies_s = [0.0]

    summary = summarize_latencies_ms(latencies_s)
    min_ms = min(latencies_s) * 1000.0
    max_ms = max(latencies_s) * 1000.0
    avg_ms = (sum(latencies_s) / len(latencies_s)) * 1000.0
    success_rate = success_count / job_count if job_count else 0.0

    row = E2EResultRow(
        suite=suite,
        test_name=test_name,
        status=status,
        concurrency=concurrency,
        job_count=job_count,
        success_count=success_count,
        success_rate=success_rate,
        p50_ms=summary["p50_ms"],
        p95_ms=summary["p95_ms"],
        p99_ms=summary["p99_ms"],
        min_ms=min_ms,
        max_ms=max_ms,
        avg_ms=avg_ms,
        sample_reply=sample_reply[:200],
        notes=notes,
    )
    path = write_e2e_result(row)
    print(f"E2E CSV appended: {path.resolve()}")
    return path

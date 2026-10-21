"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Shared helpers for live backend API load/latency probes."""


from __future__ import annotations

import csv
import json
import math
import os
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import pytest
import requests

from dermascan.tests.fixtures.payloads import telegram_text_webhook, whatsapp_text_webhook

CONFIG_PATH = Path(__file__).with_name("stress_config.json")

IngressEndpoint = Literal["health", "whatsapp", "telegram"]

DEFAULT_CONFIG: dict[str, object] = {
    "base_url": "http://localhost:8088",
    "ingress_endpoint": "whatsapp",
    "http_timeout": 30,
    "p95_concurrency": 20,
    "p95_concurrency_levels": [20, 40],
    "p95_rounds": 3,
    "p95_warmup_rounds": 1,
    "ingress_p95_slo_ms": 300,
    "enforce_ingress_slo": False,
}


@dataclass(frozen=True)
class ProbeSample:
    """One HTTP probe result."""

    status_code: int
    latency_s: float
    endpoint: str


@dataclass(frozen=True)
class LatencySummary:
    """Aggregated latency percentiles for a probe run."""

    endpoint: str
    concurrency: int
    rounds: int
    samples: int
    admission_samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    admission_p95_ms: float
    success_rate: float
    throttle_rate: float
    error_rate: float
    status_counts: dict[int, int]


def load_probe_config() -> dict[str, object]:
    """Load probe config from JSON file and environment overrides."""
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            cfg.update(json.load(handle))

    cfg["base_url"] = os.getenv("DERMASCAN_API_URL", os.getenv("DERMASCAN_GATEWAY_URL", str(cfg["base_url"]))).rstrip("/")
    cfg["ingress_endpoint"] = os.getenv("DERMASCAN_INGRESS_ENDPOINT", str(cfg["ingress_endpoint"]))
    cfg["http_timeout"] = float(os.getenv("DERMASCAN_HTTP_TIMEOUT", cfg["http_timeout"]))
    cfg["p95_concurrency"] = int(os.getenv("DERMASCAN_P95_CONCURRENCY", cfg["p95_concurrency"]))
    cfg["p95_rounds"] = int(os.getenv("DERMASCAN_P95_ROUNDS", cfg["p95_rounds"]))
    cfg["p95_warmup_rounds"] = int(os.getenv("DERMASCAN_P95_WARMUP_ROUNDS", cfg["p95_warmup_rounds"]))
    cfg["ingress_p95_slo_ms"] = float(os.getenv("DERMASCAN_INGRESS_P95_SLO_MS", cfg["ingress_p95_slo_ms"]))
    enforce = os.getenv("DERMASCAN_ENFORCE_INGRESS_SLO", str(cfg["enforce_ingress_slo"])).lower()
    cfg["enforce_ingress_slo"] = enforce in {"1", "true", "yes", "on"}
    return cfg


def load_p95_concurrency_levels(cfg: dict[str, object]) -> list[int]:
    """Resolve ingress probe concurrency levels (e.g. 20 and 40).

    Priority: ``DERMASCAN_P95_CONCURRENCY_LEVELS`` env → ``p95_concurrency_levels``
    in stress_config.json → single ``p95_concurrency`` fallback.

    Args:
        cfg: Config mapping from :func:`load_probe_config`.

    Returns:
        Sorted unique concurrency levels to probe.
    """
    levels_env = os.getenv("DERMASCAN_P95_CONCURRENCY_LEVELS", "").strip()
    if levels_env:
        levels = [int(part.strip()) for part in levels_env.split(",") if part.strip()]
    else:
        raw_levels = cfg.get("p95_concurrency_levels")
        if isinstance(raw_levels, list) and raw_levels:
            levels = [int(level) for level in raw_levels]
        else:
            levels = [int(cfg["p95_concurrency"])]

    return sorted({level for level in levels if level >= 1})


def _ingress_secret() -> str:
    """Resolve WhatsApp ingress secret from environment."""
    return os.getenv("WHATSAPP_INGRESS_SECRET", os.getenv("INTERNAL_API_SECRET", "")).strip()


def ensure_backend_api_up(base_url: str) -> None:
    """Skip the probe when the FastAPI backend is unreachable."""
    health_url = f"{base_url}/health"
    try:
        response = requests.get(health_url, timeout=5)
    except requests.RequestException as exc:  # pragma: no cover - network guard
        pytest.skip(f"Backend API not reachable at {health_url} ({exc})")
    if response.status_code != 200:
        pytest.skip(
            f"Backend health check failed at {health_url}, status {response.status_code}. "
            "Start the Dermascan backend on port 8088 "
            "(poetry run uvicorn dermascan.backend.app.main:app --port 8088) "
            "or set DERMASCAN_API_URL."
        )


def percentile_ms(latencies_s: list[float], percentile: float) -> float:
    """Compute interpolated percentile latency in milliseconds.

    Args:
        latencies_s: Observed latencies in seconds.
        percentile: Target percentile in [0, 100].

    Returns:
        Interpolated latency in milliseconds.
    """
    if not latencies_s:
        return 0.0
    ordered = sorted(latencies_s)
    if len(ordered) == 1:
        return ordered[0] * 1000.0

    rank = (percentile / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        value_s = ordered[int(rank)]
    else:
        value_s = ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower)
    return value_s * 1000.0


def _is_admission_success(status_code: int) -> bool:
    """Return True when ingress accepted or intentionally ignored the payload."""
    return status_code == 200


def _is_throttled(status_code: int) -> bool:
    return status_code == 429


def _is_error(status_code: int) -> bool:
    return status_code >= 500


def _build_request_factory(
    *,
    base_url: str,
    endpoint: IngressEndpoint,
    timeout_s: float,
) -> Callable[[requests.Session, int], ProbeSample]:
    """Build a per-sample request callable for the selected ingress endpoint."""
    if endpoint == "health":
        url = f"{base_url}/health"

        def _probe(session: requests.Session, seq: int) -> ProbeSample:
            start = time.perf_counter()
            response = session.get(url, timeout=timeout_s)
            elapsed = time.perf_counter() - start
            return ProbeSample(status_code=response.status_code, latency_s=elapsed, endpoint=endpoint)

        return _probe

    if endpoint == "whatsapp":
        url = f"{base_url}/v1/webhooks/whatsapp"
        secret = _ingress_secret()
        if not secret:
            pytest.skip("WHATSAPP_INGRESS_SECRET or INTERNAL_API_SECRET required for whatsapp ingress probe")

        def _probe(session: requests.Session, seq: int) -> ProbeSample:
            payload = whatsapp_text_webhook(
                message_id=f"wamid.p95.{uuid.uuid4().hex}",
                text=f"P95 ingress probe {seq}",
            )
            headers = {"X-Internal-Secret": secret, "Content-Type": "application/json"}
            start = time.perf_counter()
            response = session.post(url, headers=headers, json=payload, timeout=timeout_s)
            elapsed = time.perf_counter() - start
            return ProbeSample(status_code=response.status_code, latency_s=elapsed, endpoint=endpoint)

        return _probe

    if endpoint == "telegram":
        url = f"{base_url}/v1/webhooks/telegram"

        def _probe(session: requests.Session, seq: int) -> ProbeSample:
            payload = telegram_text_webhook(
                message_id=seq + int(time.time() * 1000) % 1_000_000,
                update_id=seq + int(time.time() * 1000),
                text=f"P95 ingress probe {seq}",
            )
            start = time.perf_counter()
            response = session.post(url, json=payload, timeout=timeout_s)
            elapsed = time.perf_counter() - start
            return ProbeSample(status_code=response.status_code, latency_s=elapsed, endpoint=endpoint)

        return _probe

    raise ValueError(f"Unsupported ingress endpoint: {endpoint}")


def run_ingress_probe(
    *,
    base_url: str,
    endpoint: IngressEndpoint,
    concurrency: int,
    rounds: int,
    warmup_rounds: int,
    timeout_s: float,
) -> list[ProbeSample]:
    """Execute concurrent ingress probes with optional warmup."""
    probe = _build_request_factory(base_url=base_url, endpoint=endpoint, timeout_s=timeout_s)
    samples: list[ProbeSample] = []

    def _run_round(round_index: int) -> list[ProbeSample]:
        round_samples: list[ProbeSample] = []

        def _task(seq: int) -> ProbeSample:
            # Per-thread session avoids cross-thread connection reuse skew.
            with requests.Session() as session:
                return probe(session, round_index * concurrency + seq)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_task, i) for i in range(concurrency)]
            for future in as_completed(futures):
                round_samples.append(future.result())
        return round_samples

    for _ in range(warmup_rounds):
        _run_round(-1)

    for round_index in range(rounds):
        samples.extend(_run_round(round_index))

    return samples


def summarize_samples(
    samples: list[ProbeSample],
    *,
    endpoint: str,
    concurrency: int,
    rounds: int,
) -> LatencySummary:
    """Aggregate probe samples into latency percentiles and outcome rates."""
    if not samples:
        raise ValueError("No probe samples captured")

    status_counts = Counter(sample.status_code for sample in samples)
    total = len(samples)
    latencies_s = [sample.latency_s for sample in samples]
    admission_latencies_s = [sample.latency_s for sample in samples if _is_admission_success(sample.status_code)]

    successes = sum(status_counts[code] for code in status_counts if _is_admission_success(code))
    throttled = sum(status_counts[code] for code in status_counts if _is_throttled(code))
    errors = sum(status_counts[code] for code in status_counts if _is_error(code))

    admission_p95_ms = percentile_ms(admission_latencies_s, 95) if admission_latencies_s else 0.0

    return LatencySummary(
        endpoint=endpoint,
        concurrency=concurrency,
        rounds=rounds,
        samples=total,
        admission_samples=len(admission_latencies_s),
        p50_ms=percentile_ms(latencies_s, 50),
        p95_ms=percentile_ms(latencies_s, 95),
        p99_ms=percentile_ms(latencies_s, 99),
        admission_p95_ms=admission_p95_ms,
        success_rate=successes / total,
        throttle_rate=throttled / total,
        error_rate=errors / total,
        status_counts=dict(status_counts),
    )


def write_p95_csv(summary: LatencySummary, output_path: Path) -> None:
    """Persist one ingress p95 summary row to CSV (overwrites file)."""
    write_p95_csv_rows([summary], output_path)


def write_p95_csv_rows(summaries: list[LatencySummary], output_path: Path) -> None:
    """Persist one or more ingress p95 summaries to CSV."""
    if not summaries:
        raise ValueError("No probe summaries to write")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "timestamp",
            "endpoint",
            "concurrency",
            "rounds",
            "samples",
            "admission_samples",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "admission_p95_ms",
            "success_rate",
            "throttle_rate",
            "error_rate",
            "status_counts",
        ])
        timestamp = datetime.now(timezone.utc).isoformat()
        for summary in summaries:
            writer.writerow([
                timestamp,
                summary.endpoint,
                summary.concurrency,
                summary.rounds,
                summary.samples,
                summary.admission_samples,
                f"{summary.p50_ms:.2f}",
                f"{summary.p95_ms:.2f}",
                f"{summary.p99_ms:.2f}",
                f"{summary.admission_p95_ms:.2f}",
                f"{summary.success_rate:.4f}",
                f"{summary.throttle_rate:.4f}",
                f"{summary.error_rate:.4f}",
                json.dumps(summary.status_counts),
            ])


def assert_probe_summary_ok(summary: LatencySummary, *, slo_ms: float, enforce_slo: bool) -> None:
    """Assert ingress probe outcomes are acceptable.

    Args:
        summary: Aggregated probe results.
        slo_ms: Admission p95 SLO in milliseconds.
        enforce_slo: Whether to fail when admission p95 exceeds the SLO.

    Raises:
        AssertionError: On unexpected status codes, server errors, or SLO breach.
    """
    unexpected = set(summary.status_counts) - {200, 429}
    assert not unexpected, f"Unexpected status codes during ingress probe: {unexpected}"
    assert summary.error_rate == 0.0, f"Server errors observed: {summary.status_counts}"

    if enforce_slo and summary.admission_samples > 0 and summary.throttle_rate < 0.5:
        assert summary.admission_p95_ms <= slo_ms, (
            f"Admission p95 {summary.admission_p95_ms:.1f}ms exceeds ingress SLO {slo_ms:.0f}ms "
            f"at concurrency={summary.concurrency}"
        )


def run_endpoint_probe(
    endpoint: IngressEndpoint,
    *,
    concurrency: int | None = None,
) -> LatencySummary:
    """Run ingress probes for one endpoint and return the aggregated summary.

    Args:
        endpoint: Target ingress surface.
        concurrency: Optional override; defaults to ``p95_concurrency`` in config.

    Returns:
        Latency summary for the probe run.
    """
    cfg = load_probe_config()
    base_url = str(cfg["base_url"])
    level = concurrency if concurrency is not None else int(cfg["p95_concurrency"])
    rounds = int(cfg["p95_rounds"])
    warmup_rounds = int(cfg["p95_warmup_rounds"])
    timeout_s = float(cfg["http_timeout"])

    ensure_backend_api_up(base_url)

    samples = run_ingress_probe(
        base_url=base_url,
        endpoint=endpoint,
        concurrency=level,
        rounds=rounds,
        warmup_rounds=warmup_rounds,
        timeout_s=timeout_s,
    )
    return summarize_samples(
        samples,
        endpoint=endpoint,
        concurrency=level,
        rounds=rounds,
    )


def format_summary_line(summary: LatencySummary, slo_ms: float) -> str:
    """Format a human-readable one-line summary."""
    slo_flag = "PASS" if summary.admission_p95_ms <= slo_ms else "FAIL"
    return (
        f"[{summary.endpoint} concurrency={summary.concurrency}] "
        f"admission_p95={summary.admission_p95_ms:.1f}ms "
        f"(slo={slo_ms:.0f}ms {slo_flag}) | all_p95={summary.p95_ms:.1f}ms "
        f"p50={summary.p50_ms:.1f}ms p99={summary.p99_ms:.1f}ms | "
        f"samples={summary.samples} admitted={summary.admission_samples} | "
        f"success={summary.success_rate:.1%} throttle={summary.throttle_rate:.1%} "
        f"errors={summary.error_rate:.1%} | statuses={summary.status_counts}"
    )

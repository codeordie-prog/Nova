"""Ingress p95 latency probe against the FastAPI backend API.

Measures admission latency for backend ingress endpoints — not the MedGemma
/diagnose inference path. Targets the surfaces defined in tests.md:

- GET  /health
- POST /v1/webhooks/whatsapp
- POST /v1/webhooks/telegram

SLO placeholder: admission p95 < 300 ms (successful 200 responses only).
429 throttles are reported separately and excluded from admission p95.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dermascan.tests.API.probe_helpers import (
    IngressEndpoint,
    ensure_backend_api_up,
    format_summary_line,
    load_probe_config,
    run_ingress_probe,
    summarize_samples,
    write_p95_csv,
)

OUTPUT_CSV = Path(os.getenv("DERMASCAN_INGRESS_P95_CSV", Path(__file__).with_name("ingress_p95_results.csv")))


def _run_endpoint_probe(endpoint: IngressEndpoint) -> None:
    """Run p95 probe for a single ingress endpoint and persist results."""
    cfg = load_probe_config()
    base_url = str(cfg["base_url"])
    concurrency = int(cfg["p95_concurrency"])
    rounds = int(cfg["p95_rounds"])
    warmup_rounds = int(cfg["p95_warmup_rounds"])
    timeout_s = float(cfg["http_timeout"])
    slo_ms = float(cfg["ingress_p95_slo_ms"])
    enforce_slo = bool(cfg["enforce_ingress_slo"])

    ensure_backend_api_up(base_url)

    samples = run_ingress_probe(
        base_url=base_url,
        endpoint=endpoint,
        concurrency=concurrency,
        rounds=rounds,
        warmup_rounds=warmup_rounds,
        timeout_s=timeout_s,
    )
    summary = summarize_samples(
        samples,
        endpoint=endpoint,
        concurrency=concurrency,
        rounds=rounds,
    )

    print(format_summary_line(summary, slo_ms))
    write_p95_csv(summary, OUTPUT_CSV)
    print(f"CSV written to: {OUTPUT_CSV.resolve()}")

    unexpected = set(summary.status_counts) - {200, 429}
    assert not unexpected, f"Unexpected status codes during ingress probe: {unexpected}"
    assert summary.error_rate == 0.0, f"Server errors observed: {summary.status_counts}"

    if enforce_slo and summary.admission_samples > 0 and summary.throttle_rate < 0.5:
        assert summary.admission_p95_ms <= slo_ms, (
            f"Admission p95 {summary.admission_p95_ms:.1f}ms exceeds ingress SLO {slo_ms:.0f}ms"
        )


@pytest.mark.load
def test_ingress_p95_whatsapp() -> None:
    """P95 latency for POST /v1/webhooks/whatsapp (text enqueue path)."""
    _run_endpoint_probe("whatsapp")


@pytest.mark.load
def test_ingress_p95_telegram() -> None:
    """P95 latency for POST /v1/webhooks/telegram (text enqueue path)."""
    _run_endpoint_probe("telegram")


@pytest.mark.load
def test_ingress_p95_health_baseline() -> None:
    """P95 latency baseline for GET /health (no Redis/admission work)."""
    _run_endpoint_probe("health")


@pytest.mark.load
def test_ingress_p95_configured_endpoint() -> None:
    """P95 latency for the endpoint selected in stress_config / DERMASCAN_INGRESS_ENDPOINT."""
    cfg = load_probe_config()
    endpoint = str(cfg["ingress_endpoint"])
    if endpoint not in {"health", "whatsapp", "telegram"}:
        pytest.skip(f"Unsupported ingress endpoint in config: {endpoint}")
    _run_endpoint_probe(endpoint)  # type: ignore[arg-type]

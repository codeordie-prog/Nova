"""Ingress p95 latency probe against the FastAPI backend API.

Measures admission latency for backend ingress endpoints — not the MedGemma
/diagnose inference path. Targets the surfaces defined in tests.md:

- GET  /health
- POST /v1/webhooks/whatsapp
- POST /v1/webhooks/telegram

Default sweep: concurrency levels **20 and 40** (configurable via
``stress_config.json`` or ``DERMASCAN_P95_CONCURRENCY_LEVELS``).

SLO placeholder: admission p95 < 300 ms (successful 200 responses only).
429 throttles are reported separately and excluded from admission p95.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dermascan.tests.API.probe_helpers import (
    IngressEndpoint,
    assert_probe_summary_ok,
    format_summary_line,
    load_p95_concurrency_levels,
    load_probe_config,
    run_endpoint_probe,
    write_p95_csv_rows,
)

OUTPUT_CSV = Path(os.getenv("DERMASCAN_INGRESS_P95_CSV", Path(__file__).with_name("ingress_p95_results.csv")))


def _run_endpoint_probe(endpoint: IngressEndpoint, *, concurrency: int | None = None) -> None:
    """Run p95 probe for a single ingress endpoint and persist results."""
    cfg = load_probe_config()
    slo_ms = float(cfg["ingress_p95_slo_ms"])
    enforce_slo = bool(cfg["enforce_ingress_slo"])

    summary = run_endpoint_probe(endpoint, concurrency=concurrency)
    print(format_summary_line(summary, slo_ms))
    write_p95_csv_rows([summary], OUTPUT_CSV)
    print(f"CSV written to: {OUTPUT_CSV.resolve()}")
    assert_probe_summary_ok(summary, slo_ms=slo_ms, enforce_slo=enforce_slo)


def _run_endpoint_probe_levels(endpoint: IngressEndpoint) -> None:
    """Run p95 probes at each configured concurrency level and persist all rows."""
    cfg = load_probe_config()
    slo_ms = float(cfg["ingress_p95_slo_ms"])
    enforce_slo = bool(cfg["enforce_ingress_slo"])
    levels = load_p95_concurrency_levels(cfg)

    summaries = []
    for level in levels:
        summary = run_endpoint_probe(endpoint, concurrency=level)
        print(format_summary_line(summary, slo_ms))
        assert_probe_summary_ok(summary, slo_ms=slo_ms, enforce_slo=enforce_slo)
        summaries.append(summary)

    write_p95_csv_rows(summaries, OUTPUT_CSV)
    print(f"CSV written ({len(summaries)} levels) to: {OUTPUT_CSV.resolve()}")


@pytest.mark.load
def test_ingress_p95_whatsapp() -> None:
    """P95 admission latency for WhatsApp webhook at concurrency 20 and 40."""
    _run_endpoint_probe_levels("whatsapp")


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
    if endpoint == "whatsapp":
        _run_endpoint_probe_levels("whatsapp")
    else:
        _run_endpoint_probe(endpoint)  # type: ignore[arg-type]

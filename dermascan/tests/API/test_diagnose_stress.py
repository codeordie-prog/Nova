import math
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import pytest
import requests

BASE_URL: str = os.getenv("DERMASCAN_GATEWAY_URL", "http://localhost:8080").rstrip("/")
DIAGNOSE_URL: str = f"{BASE_URL}/diagnose"
HTTP_TIMEOUT: float = float(os.getenv("DERMASCAN_HTTP_TIMEOUT", "120"))
PROMPT: str = os.getenv("DERMASCAN_TEST_PROMPT", "Test skin condition")
DEFAULT_LEVELS: list[int] = [5, 10, 20, 40, 60, 80]
DEFAULT_P95_CONCURRENCY: int = int(os.getenv("DERMASCAN_P95_CONCURRENCY", "20"))
DEFAULT_P95_ROUNDS: int = int(os.getenv("DERMASCAN_P95_ROUNDS", "3"))

IMAGES_DIR: Path = Path(__file__).resolve().parent.parent / "images"


def _load_image_bytes() -> list[bytes]:
    if not IMAGES_DIR.exists():
        pytest.skip(f"Sample images dir missing: {IMAGES_DIR}")

    images = [path.read_bytes() for path in IMAGES_DIR.glob("*.jpg")]
    if not images:
        pytest.skip(f"No sample images found in {IMAGES_DIR}")
    return images


def _ensure_gateway_up() -> None:
    try:
        resp = requests.get(f"{BASE_URL}/docs", timeout=5)
    except requests.RequestException as exc:  # pragma: no cover - network guard
        pytest.skip(
            f"Gateway not reachable at {BASE_URL}. Start the FastAPI gateway before running tests. ({exc})"
        )
    if resp.status_code >= 500:
        pytest.skip(f"Gateway unhealthy at {BASE_URL}, status {resp.status_code}")


def _post_diagnose(session: requests.Session, image_bytes: bytes) -> tuple[int, float]:
    files = {"image": ("sample.jpg", image_bytes, "image/jpeg")}
    data = {"prompt": PROMPT}
    start = time.perf_counter()
    response = session.post(DIAGNOSE_URL, files=files, data=data, timeout=HTTP_TIMEOUT)
    elapsed = time.perf_counter() - start
    return response.status_code, elapsed


def _run_concurrent_requests(concurrency: int, images: list[bytes]) -> list[tuple[int, float]]:
    session = requests.Session()
    results: list[tuple[int, float]] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_post_diagnose, session, images[idx % len(images)]) for idx in range(concurrency)]
        for future in as_completed(futures):
            results.append(future.result())

    return results


def _percentile(values: Iterable[float], percentile: float) -> float:
    data = sorted(values)
    if not data:
        return 0.0
    index = max(0, min(len(data) - 1, math.ceil(percentile / 100 * len(data)) - 1))
    return data[index]


def test_diagnose_concurrency_window_reports_limit() -> None:
    """
    Sends batches with increasing concurrency and reports where 429s begin to appear.

    This is meant as a quick sanity/stress probe rather than a strict SLA assertion.
    """

    _ensure_gateway_up()
    images = _load_image_bytes()

    levels_env = os.getenv("DERMASCAN_CONCURRENCY_LEVELS")
    levels: list[int] = (
        [int(x) for x in levels_env.split(",") if x.strip()]
        if levels_env
        else DEFAULT_LEVELS
    )

    observations: list[tuple[int, Counter[int], float]] = []
    for level in levels:
        batch_results = _run_concurrent_requests(level, images)
        status_counts: Counter[int] = Counter(code for code, _ in batch_results)
        avg_latency = sum(lat for _, lat in batch_results) / len(batch_results)
        observations.append((level, status_counts, avg_latency))

    for level, counts, _ in observations:
        unexpected = set(counts) - {200, 429}
        assert not unexpected, f"Unexpected status codes at concurrency {level}: {unexpected}"

    throttled_level = next((lvl for lvl, counts, _ in observations if counts.get(429, 0) > 0), None)

    for level, counts, avg_latency in observations:
        total = sum(counts.values())
        print(
            f"[concurrency {level:>3}] total={total:>3} success={counts.get(200, 0):>3} "
            f"429={counts.get(429, 0):>3} avg_latency={avg_latency:.2f}s"
        )

    if throttled_level is None:
        print(
            f"No throttling observed up to max concurrency tested ({max(levels)}). "
            "Increase DERMASCAN_CONCURRENCY_LEVELS to probe higher."
        )
    else:
        print(f"First 429s observed at concurrency >= {throttled_level}.")


def test_diagnose_p95_latency_under_load() -> None:
    """
    Captures P95 latency while sending concurrent requests.

    Uses successes when available; falls back to all responses if everything is throttled.
    """

    _ensure_gateway_up()
    images = _load_image_bytes()

    concurrency: int = DEFAULT_P95_CONCURRENCY
    rounds: int = DEFAULT_P95_ROUNDS

    all_results: list[tuple[int, float]] = []
    for _ in range(rounds):
        all_results.extend(_run_concurrent_requests(concurrency, images))

    assert all_results, "No responses captured from /diagnose"

    status_counts: Counter[int] = Counter(code for code, _ in all_results)
    unexpected = set(status_counts) - {200, 429}
    assert not unexpected, f"Unexpected status codes during latency run: {unexpected}"

    success_latencies = [lat for code, lat in all_results if code == 200]
    observed_latencies = success_latencies or [lat for _, lat in all_results]

    p95_latency = _percentile(observed_latencies, 95)
    success_rate = status_counts.get(200, 0) / sum(status_counts.values())

    print(
        f"P95 latency={'{:.2f}'.format(p95_latency)}s over {len(observed_latencies)} samples "
        f"(success rate={success_rate:.2%}, concurrency={concurrency}, rounds={rounds})"
    )
    print(f"Status breakdown: {dict(status_counts)}")

import csv
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pytest
import requests

CONFIG_PATH = Path(__file__).with_name("stress_config.json")
DEFAULT_CONFIG = {
    "base_url": "http://localhost:8080",
    "prompt": "Test skin condition",
    "http_timeout": 120,
    "p95_concurrency": 20,
    "p95_rounds": 3,
}
OUTPUT_CSV = Path(os.getenv("DERMASCAN_STRESS_P95_CSV", Path(__file__).with_name("stress_p95_results.csv")))


def _load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    # Env overrides
    cfg["base_url"] = os.getenv("DERMASCAN_GATEWAY_URL", cfg["base_url"]).rstrip("/")
    cfg["prompt"] = os.getenv("DERMASCAN_TEST_PROMPT", cfg["prompt"])
    cfg["http_timeout"] = float(os.getenv("DERMASCAN_HTTP_TIMEOUT", cfg["http_timeout"]))
    cfg["p95_concurrency"] = int(os.getenv("DERMASCAN_P95_CONCURRENCY", cfg["p95_concurrency"]))
    cfg["p95_rounds"] = int(os.getenv("DERMASCAN_P95_ROUNDS", cfg["p95_rounds"]))
    return cfg


CFG = _load_config()
DIAGNOSE_URL = f"{CFG['base_url']}/diagnose"
IMAGES_DIR = Path(__file__).resolve().parent.parent / "images"


def _load_images():
    if not IMAGES_DIR.exists():
        pytest.skip(f"Sample images dir missing: {IMAGES_DIR}")
    images = [p.read_bytes() for p in IMAGES_DIR.glob("*.jpg")]
    if not images:
        pytest.skip(f"No sample images in {IMAGES_DIR}")
    return images


def _ensure_gateway_up():
    try:
        resp = requests.get(f"{CFG['base_url']}/docs", timeout=5)
    except requests.RequestException as exc:  # pragma: no cover - network guard
        pytest.skip(f"Gateway not reachable at {CFG['base_url']} ({exc})")
    if resp.status_code >= 500:
        pytest.skip(f"Gateway unhealthy at {CFG['base_url']} status {resp.status_code}")


def _post_diagnose(session: requests.Session, image_bytes: bytes) -> tuple[int, float]:
    files = {"image": ("sample.jpg", image_bytes, "image/jpeg")}
    data = {"prompt": CFG["prompt"]}
    start = time.perf_counter()
    response = session.post(DIAGNOSE_URL, files=files, data=data, timeout=CFG["http_timeout"])
    elapsed = time.perf_counter() - start
    return response.status_code, elapsed


def _run_concurrent(concurrency: int, images, rounds: int):
    session = requests.Session()
    results = []
    for _ in range(rounds):
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_post_diagnose, session, images[i % len(images)]) for i in range(concurrency)]
            for fut in as_completed(futures):
                results.append(fut.result())
    return results


def _percentile(data, percentile):
    data = sorted(data)
    if not data:
        return 0.0
    import math

    idx = max(0, min(len(data) - 1, math.ceil(percentile / 100 * len(data)) - 1))
    return data[idx]


def _write_csv(p95_latency, success_rate, status_counts, observed_samples):
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "concurrency",
            "rounds",
            "p95_latency_s",
            "success_rate",
            "samples",
            "status_counts",
        ])
        writer.writerow([
            datetime.utcnow().isoformat(),
            CFG["p95_concurrency"],
            CFG["p95_rounds"],
            f"{p95_latency:.4f}",
            f"{success_rate:.4f}",
            observed_samples,
            json.dumps(status_counts),
        ])


def test_diagnose_p95_latency_under_load():
    """
    Captures P95 latency while sending concurrent requests.
    Prints P95 + success rate + status breakdown.
    """

    _ensure_gateway_up()
    images = _load_images()

    all_results = _run_concurrent(CFG["p95_concurrency"], images, CFG["p95_rounds"])
    assert all_results, "No responses captured from /diagnose"

    status_counts = Counter(code for code, _ in all_results)
    unexpected = set(status_counts) - {200, 429}
    assert not unexpected, f"Unexpected status codes during latency run: {unexpected}"

    success_latencies = [lat for code, lat in all_results if code == 200]
    observed_latencies = success_latencies or [lat for _, lat in all_results]

    p95_latency = _percentile(observed_latencies, 95)
    success_rate = status_counts.get(200, 0) / sum(status_counts.values())

    print(
        f"P95 latency={p95_latency:.2f}s over {len(observed_latencies)} samples "
        f"(success rate={success_rate:.2%}, concurrency={CFG['p95_concurrency']}, rounds={CFG['p95_rounds']})"
    )
    print(f"Status breakdown: {dict(status_counts)}")

    _write_csv(p95_latency, success_rate, dict(status_counts), len(observed_latencies))

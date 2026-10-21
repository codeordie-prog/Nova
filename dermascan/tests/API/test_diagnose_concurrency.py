"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly."""

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
    "concurrency_levels": [5, 10, 20, 40, 60, 80],
    "bucket_pause_seconds": 0,
}
OUTPUT_CSV = Path(os.getenv("DERMASCAN_STRESS_CSV", Path(__file__).with_name("stress_results.csv")))


def _load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    # Env overrides
    cfg["base_url"] = os.getenv("DERMASCAN_GATEWAY_URL", cfg["base_url"]).rstrip("/")
    cfg["prompt"] = os.getenv("DERMASCAN_TEST_PROMPT", cfg["prompt"])
    cfg["http_timeout"] = float(os.getenv("DERMASCAN_HTTP_TIMEOUT", cfg["http_timeout"]))
    levels_env = os.getenv("DERMASCAN_CONCURRENCY_LEVELS")
    if levels_env:
        cfg["concurrency_levels"] = [int(x) for x in levels_env.split(",") if x.strip()]
    cfg["bucket_pause_seconds"] = float(os.getenv("DERMASCAN_BUCKET_SLEEP", cfg.get("bucket_pause_seconds", 0)))
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


def _run_concurrent(level: int, images):
    session = requests.Session()
    results = []
    with ThreadPoolExecutor(max_workers=level) as executor:
        futures = [executor.submit(_post_diagnose, session, images[i % len(images)]) for i in range(level)]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def _write_csv(observations):
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "concurrency",
            "total",
            "success_200",
            "throttled_429",
            "other",
            "avg_latency_s",
        ])
        ts = datetime.utcnow().isoformat()
        for level, counts, avg_latency in observations:
            total = sum(counts.values())
            throttled = counts.get(429, 0)
            success = counts.get(200, 0)
            other = total - throttled - success
            writer.writerow([ts, level, total, success, throttled, other, f"{avg_latency:.4f}"])


def test_diagnose_concurrency_window_reports_limit():
    """
    Sends batches at increasing concurrency and reports where 429s appear.
    Prints: total, successes, 429s, other errors, avg latency per level.
    """

    _ensure_gateway_up()
    images = _load_images()

    observations: list[tuple[int, Counter, float]] = []
    try:
        for level in CFG["concurrency_levels"]:
            print(f"\n>>> Running concurrency level {level} …")
            batch = _run_concurrent(level, images)
            counts = Counter(code for code, _ in batch)
            avg_latency = sum(lat for _, lat in batch) / len(batch)
            observations.append((level, counts, avg_latency))

            pause = CFG.get("bucket_pause_seconds", 0) or 0
            if pause > 0:
                print(f"    pausing {pause}s between buckets …")
                time.sleep(pause)
    finally:
        # Always persist whatever observations we collected
        if observations:
            _write_csv(observations)
            print(f"\n>>> CSV report written to: {OUTPUT_CSV.resolve()}")
        else:
            print("\n>>> No observations collected — CSV not written.")

    for level, counts, avg_latency in observations:
        total = sum(counts.values())
        throttled = counts.get(429, 0)
        success = counts.get(200, 0)
        other = total - throttled - success
        print(
            f"[concurrency {level:>3}] total={total:>4} 200={success:>4} 429={throttled:>4} "
            f"other={other:>4} avg_latency={avg_latency:.2f}s"
        )
        unexpected = set(counts) - {200, 429}
        assert not unexpected, f"Unexpected status codes at concurrency {level}: {unexpected}"

    first_throttled = next((lvl for lvl, counts, _ in observations if counts.get(429, 0) > 0), None)
    if first_throttled is None:
        print(
            f"\nNo throttling observed up to max concurrency tested ({max(CFG['concurrency_levels'])}). "
            "Increase DERMASCAN_CONCURRENCY_LEVELS or config to probe higher."
        )
    else:
        print(f"\nFirst 429s observed at concurrency >= {first_throttled}.")

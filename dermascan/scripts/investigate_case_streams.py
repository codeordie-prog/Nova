#!/usr/bin/env python3
"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Inspect Redis streams for duplicate publishes for a case/request id."""


from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any

from dermascan.shared.load_env import bootstrap_env

bootstrap_env()

CASE_ID = sys.argv[1] if len(sys.argv) > 1 else "c7bd36488a96421cb4f2150a96537646"

SIGNAL_STREAM = os.getenv("ORCHESTRATOR_SIGNAL_STREAM", "orchestrator:signals")
EVENTS_STREAM = os.getenv("EVENTS_STREAM", "events:stream")
MEDGEMMA_STREAM = os.getenv("MEDGEMMA_JOB_STREAM", os.getenv("DIAGNOSE_JOB_STREAM", "medgemma:jobs"))
ESCALATION_STREAM = os.getenv("ESCALATION_STREAM", "escalation:jobs")


def _norm(fields: dict[Any, Any]) -> dict[str, str]:
    return {str(k): "" if v is None else str(v) for k, v in fields.items()}


def _scan_stream(redis_client: Any, stream: str, *, limit: int = 5000) -> list[tuple[str, dict[str, str]]]:
    rows: list[tuple[str, dict[str, str]]] = []
    start = "-"
    while len(rows) < limit:
        batch = redis_client.xrange(stream, min=start, max="+", count=200)
        if not batch:
            break
        for msg_id, fields in batch:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            if rows and mid == rows[-1][0]:
                continue
            rows.append((mid, _norm(fields)))
        if len(batch) < 200:
            break
        start = f"({rows[-1][0]}"
    return rows


def main() -> None:
    import redis

    url = os.getenv("REDIS_URL", "")
    if not url:
        print("REDIS_URL not set")
        sys.exit(1)

    r = redis.from_url(url, decode_responses=True)
    print("redis ping:", r.ping())
    print("case/request filter:", CASE_ID)
    print()

    for stream in (MEDGEMMA_STREAM, SIGNAL_STREAM, EVENTS_STREAM, ESCALATION_STREAM):
        print("=" * 72)
        print("STREAM", stream)
        matches: list[tuple[str, dict[str, str]]] = []
        try:
            for mid, fields in _scan_stream(r, stream):
                blob = json.dumps(fields)
                if CASE_ID in blob or CASE_ID in fields.get("request_id", "") or CASE_ID in fields.get("case_id", ""):
                    matches.append((mid, fields))
        except Exception as exc:
            print("  error:", exc)
            continue

        print(f"  matches: {len(matches)}")
        for mid, fields in matches:
            interesting = {
                k: fields[k]
                for k in (
                    "signal_type",
                    "request_id",
                    "case_id",
                    "client_id",
                    "job_id",
                    "status",
                    "mode",
                    "agent",
                    "reason",
                    "confidence",
                    "threshold",
                )
                if k in fields and fields[k]
            }
            print(f"  - {mid} {interesting}")

    print()
    print("=" * 72)
    print("DUPLICATE SIGNAL TYPES (same request_id + signal_type)")
    by_key: dict[str, list[str]] = defaultdict(list)
    for mid, fields in _scan_stream(r, SIGNAL_STREAM):
        if fields.get("request_id") != CASE_ID and fields.get("case_id") != CASE_ID:
            continue
        key = f"{fields.get('request_id')}::{fields.get('signal_type')}"
        by_key[key].append(mid)
    for key, ids in sorted(by_key.items()):
        if len(ids) > 1:
            print(f"  DUPLICATE {key} -> {ids}")


if __name__ == "__main__":
    main()

"""Probe 360dialog HTTPS reachability the same way the sidecar does."""

from __future__ import annotations

import asyncio
import time

import httpx


async def probe(label: str, **client_kwargs: object) -> None:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(
                "https://waba-v2.360dialog.io/messages",
                headers={"D360-API-KEY": "probe"},
            )
            elapsed = time.perf_counter() - start
            print(f"{label}: HTTP {response.status_code} in {elapsed:.2f}s")
    except Exception as exc:
        elapsed = time.perf_counter() - start
        print(f"{label}: {type(exc).__name__} in {elapsed:.2f}s")


async def main() -> None:
    await probe("default", timeout=15.0)
    await probe("ipv4", timeout=15.0, local_address="0.0.0.0")


if __name__ == "__main__":
    asyncio.run(main())

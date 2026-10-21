#!/usr/bin/env python3
"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Add REDIS_HOST to .env when only REDIS_URL is configured."""


from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def _repo_root() -> Path:
    cursor = Path(__file__).resolve().parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not find repo root")


def main() -> None:
    env_path = _repo_root() / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    if values.get("REDIS_HOST", "").strip():
        print("REDIS_HOST already set; no change")
        return

    redis_url = values.get("REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is missing from .env")

    hostname = urlparse(redis_url).hostname
    if not hostname:
        raise RuntimeError("Could not parse hostname from REDIS_URL")

    host_line = f"REDIS_HOST={hostname}"
    output: list[str] = []
    inserted = False
    for line in lines:
        if line.strip().startswith("REDIS_PORT=") and not inserted:
            output.append(host_line)
            inserted = True
        output.append(line)
    if not inserted:
        output.append(host_line)

    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(f"Added {host_line}")


if __name__ == "__main__":
    main()

"""JSON helpers for logs: pretty-printed, valid JSON (readable in terminals)."""

from __future__ import annotations

import json
from typing import Any


def dumps_compact(obj: Any) -> str:
    """Single-line JSON (e.g. metrics); survives datetime / arbitrary nested structs."""
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def dumps_pretty(obj: Any, *, indent: int = 2) -> str:
    """Multi-line indented JSON for log readability."""
    return json.dumps(obj, ensure_ascii=False, default=str, indent=indent)
